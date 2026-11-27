"""Serve the shared Isaac simulation wire contract on the A100 gateway."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

import grpc
from google.protobuf.json_format import MessageToDict, ParseDict

from a100sim import MAX_IMAGE_SIZE, MAX_SAMPLES_PER_PIXEL
from a100sim.rpc.catalog import API_VERSION, SIMULATOR_VERSION, build_catalog, resolve_feature
from a100sim.rpc.codec import Task, validate_numbers
from a100sim.rpc.errors import ServiceError
from a100sim.rpc.v1 import simulator_pb2 as legacy
from a100sim.rpc.storage import AssetStore
from rtx4090_grpc.server import codec, rpc
from isaacsim_rpc.v1 import native_pb2 as native

pb = rpc.simulation_pb2
SERVICE = pb.DESCRIPTOR.services_by_name["SimulationService"].full_name


class UnifiedService(rpc.simulation_pb2_grpc.SimulationServiceServicer):
    def __init__(self, manager, handle_errors):
        self.manager = manager
        self.handle_errors = handle_errors

    def _record(self, identity):
        if not re.fullmatch("[a-f0-9]{32}", identity):
            raise ValueError("job_id must be 32 lower-case hexadecimal characters")
        return self.manager.get_record(identity)

    @staticmethod
    def _convert(message, message_type):
        """Copy structurally identical native messages without exposing a second RPC service."""
        result = message_type()
        ParseDict(MessageToDict(message, preserving_proto_field_name=True), result)
        return result

    def _job(self, record, native_snapshot=None):
        result = record.get("result", {})
        timings = record.get("timings", {})
        state = record["state"].lower()
        if state == "cancelled":
            state = "interrupted"
        job = codec.job_to_proto({
            **result, "id": record["job_id"], "state": state, "queued_at": record["created_at_unix"],
            "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
            "phase": record.get("phase", state) if state == "running" else state,
            "frames_done": record.get("frames_done", len(result.get("frames", []))),
            "frames_total": record.get("frames_total", len(result.get("frames", []))),
            "queue_seconds": timings.get("gateway_queue_seconds"),
            "execution_seconds": timings.get("total_seconds", 0) - timings.get("gateway_queue_seconds", 0),
            "error": record.get("failure", {}).get("message", ""), "artifact_manifest": record.get("artifacts", []),
        })
        if not record.get("unified"):
            receipt = native_snapshot or self.manager.encode(record)
            job.native_job.CopyFrom(self._convert(receipt, native.Job))
        return job

    @staticmethod
    def _request_id(outer, inner):
        if outer and inner and outer != inner:
            raise ValueError("outer request_id must match the native task request_id")
        identity = outer or inner
        if not identity:
            raise ValueError("request_id is required")
        return identity

    def _task(self, simulation):
        validate_numbers(simulation)
        payload = codec.simulate_request_to_payload(simulation)
        for body in payload.get("bodies", []):
            body.setdefault("mass", 0.0)
            handle = body.pop("asset_id", "")
            if body.get("asset") and handle:
                raise ValueError("Choose one of asset or asset_id")
            if handle:
                body["asset"] = str(self.manager.assets.resolve(handle))
            elif body.get("asset"):
                root = self.manager.assets.root
                path = Path(body["asset"]).resolve()
                if not path.is_relative_to(root) or not path.is_dir():
                    raise ValueError("asset must be a directory inside the configured asset root")
                body["asset"] = str(path)
        if "capture" in payload and "render" not in payload:
            payload["render"] = {}
        if "render" in payload:
            payload["route"] = "generic"
            if payload["render"].get("kind", "mesh") == "mesh":
                payload["render"]["annotations"] = True
        return Task("simulate", payload)

    async def Health(self, request, context):
        async with self.handle_errors(context):
            backend = self.manager.backend
            probe = getattr(backend, "health", None)
            if probe:
                workers = await probe()
                worker = next((workers[key] for key in ("generic", "resident")
                               if workers.get(key, {}).get("status") == "ready"), {})
                ready = bool(worker)
            else:
                worker = {}
                ready = any(item.name == "simulate" and item.availability == legacy.AVAILABLE
                            for item in await backend.describe())
            records = self.manager.records.values()
            root = self.manager.settings.state_root
            variant = worker.get("render_variant") or os.environ.get("A100SIM_RENDER_VARIANT", "scalar_rgb")
            return codec.health_to_proto({
                **worker, "status": "ready" if ready else "failed", "engine": "a100sim",
                "native_rtx": False, "cuda_required": True, "cpu_fallback": variant != "cuda_ad_rgb",
                "render_backend": variant, "renderer": "Mitsuba", "supported_shapes": ["cube", "sphere", "mesh"],
                "annotators": ["rgb", "depth_m", "semantic_ids", "instance_ids"], "api_version": "1.0.0",
                "max_views": 32,
                "max_image_size": MAX_IMAGE_SIZE, "max_samples_per_pixel": MAX_SAMPLES_PER_PIXEL,
                "capabilities": [
                    "multiview_final", "multiview_frames", "metric_depth", "instance_ids",
                    "semantic_masks", "visible_bboxes", "registered_mesh_npz", "native_task_api",
                    *(["native_operations"] if os.environ.get("A100SIM_SCENE_URL") else []),
                ],
                "uptime_seconds": time.monotonic() - self.manager.started_at,
                "queue_depth": self.manager.queue.qsize(), "queue_capacity": self.manager.settings.capacity,
                "active_job": next((r["job_id"] for r in records if r["state"] == "RUNNING"), ""),
                "completed": sum(r["state"] == "SUCCEEDED" for r in records),
                "failed": sum(r["state"] == "FAILED" for r in records),
                "artifacts_root": str(root / "downloads"), "artifacts_free_bytes": shutil.disk_usage(root).free,
                "retention_seconds": self.manager.settings.retention_seconds,
            })

    async def GetCapabilities(self, request, context):
        async with self.handle_errors(context):
            values = await self.manager.backend.describe()
            capabilities = [self._convert(item, native.Capability) for item in values]
            capabilities.append(native.Capability(
                name="native_task_api", availability=native.AVAILABLE, scope="A100 typed simulation tasks"
            ))
            if os.environ.get("A100SIM_SCENE_URL"):
                health = await self.manager.backend.health()
                scene_ready = health.get("scenes", {}).get("status") == "ready"
                capabilities.append(native.Capability(
                    name="native_operations",
                    availability=native.AVAILABLE if scene_ready else native.UNAVAILABLE,
                    reason="" if scene_ready else health.get("scenes", {}).get(
                        "reason", "Persistent scene worker is not ready"
                    ),
                    scope="Persistent scene operations",
                ))
            return native.Capabilities(
                api_version=API_VERSION,
                simulator_version=SIMULATOR_VERSION,
                native_rtx=False,
                capabilities=capabilities,
                queue_capacity=self.manager.settings.capacity,
                queued_jobs=self.manager.queue.qsize(),
                running_jobs=sum(record["state"] == "RUNNING" for record in self.manager.records.values()),
            )

    async def GetApiCatalog(self, request, context):
        async with self.handle_errors(context):
            return self._convert(build_catalog(await self.manager.backend.describe()), native.ApiCatalog)

    async def ListAssets(self, request, context):
        async with self.handle_errors(context):
            store = AssetStore(self.manager.assets.root, self.manager.settings.max_upload_bytes, messages=native)
            return await asyncio.to_thread(store.list_assets)

    async def UploadAsset(self, requests, context):
        async with self.handle_errors(context):
            store = AssetStore(self.manager.assets.root, self.manager.settings.max_upload_bytes, messages=native)
            return await store.upload(requests)

    async def SubmitJob(self, request, context):
        async with self.handle_errors(context):
            workload = request.WhichOneof("workload")
            if workload == "simulation":
                job = self.manager.submit_simulation(
                    request.request_id,
                    request.simulation,
                    lambda: self._task(request.simulation),
                )
            elif workload == "native_task":
                task = self._convert(request.native_task, legacy.SubmitTaskRequest)
                identity = self._request_id(request.request_id, task.request_id)
                task.request_id = identity
                job = await self.manager.submit(task, snapshot_artifacts=True)
            elif workload == "native_feature":
                feature = self._convert(request.native_feature, legacy.FeatureRequest)
                identity = self._request_id(request.request_id, feature.request_id)
                feature.request_id = identity
                job = await self.manager.submit(resolve_feature(feature), snapshot_artifacts=True)
            else:
                raise ValueError("Exactly one simulation or native task is required")
            record = self._record(job.job_id)
            response = self._job(record)
            return pb.JobHandle(id=job.job_id, state=response.state, queued_at=record["created_at_unix"])

    async def GetJob(self, request, context):
        async with self.handle_errors(context):
            return self._job(self._record(request.job_id))

    async def WatchJob(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            async for snapshot in self.manager.observe(request.job_id, request.after_revision):
                yield self._job(record, native_snapshot=snapshot if not record.get("unified") else None)

    async def CancelJob(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            if record["state"] == "RUNNING" and record.get("unified"):
                async with self.manager.changed:
                    self.manager._update(record, cancel_requested=True)
                    self.manager.changed.notify_all()
            else:
                await self.manager.cancel(request.job_id)
            return self._job(record)

    async def AckJob(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            if not (record.get("unified") or record.get("snapshot_artifacts")):
                raise ServiceError(
                    grpc.StatusCode.FAILED_PRECONDITION,
                    "Historical native artifacts are not owned snapshots and cannot be released through AckJob",
                )
            if record["state"] != "SUCCEEDED":
                raise ServiceError(grpc.StatusCode.FAILED_PRECONDITION, "Job has not succeeded")
            if self.manager.downloads.get(request.job_id, 0):
                raise ServiceError(grpc.StatusCode.RESOURCE_EXHAUSTED, "Artifact downloads are still active; retry AckJob")
            released, size = self.manager.release_artifacts(record)
            return pb.AckJobResponse(released=released, freed_bytes=size)

    async def DownloadArtifact(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            if record["state"] != "SUCCEEDED":
                raise ServiceError(grpc.StatusCode.FAILED_PRECONDITION, "Job has not succeeded")
            item = next((a for a in record["artifacts"] if a["artifact_id"] == request.artifact_id), None)
            if item is None or record.get("released"):
                raise ServiceError(grpc.StatusCode.NOT_FOUND, "Unknown or released artifact")
            offset = request.offset
            if offset > item["size_bytes"]:
                raise ServiceError(grpc.StatusCode.OUT_OF_RANGE, "Offset exceeds artifact size")
            active = self.manager.downloads.get(request.job_id, 0)
            if active >= 8:
                raise ServiceError(grpc.StatusCode.RESOURCE_EXHAUSTED, "Too many downloads for this job")
            self.manager.downloads[request.job_id] = active + 1
            try:
                with Path(item["path"]).open("rb") as stream:
                    stream.seek(offset)
                    chunk_size = request.chunk_bytes or 256 * 1024
                    if not 1 <= chunk_size <= 1024 * 1024:
                        raise ValueError("Chunk size must be 1 byte–1 MiB")
                    while block := await asyncio.to_thread(stream.read, chunk_size):
                        yield pb.DownloadChunk(offset=offset, data=block)
                        offset += len(block)
            finally:
                self.manager.downloads[request.job_id] -= 1
                if not self.manager.downloads[request.job_id]:
                    del self.manager.downloads[request.job_id]
