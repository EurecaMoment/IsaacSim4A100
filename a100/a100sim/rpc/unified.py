"""Serve the shared Isaac simulation wire contract on the A100 gateway."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

import grpc

from a100sim.rpc.codec import Task, validate_numbers
from a100sim.rpc.errors import ServiceError
from a100sim.rpc.v1 import simulator_pb2 as legacy
from rtx4090_grpc.server import codec, rpc

pb = rpc.simulation_pb2
SERVICE = pb.DESCRIPTOR.services_by_name["SimulationService"].full_name


class UnifiedService(rpc.simulation_pb2_grpc.SimulationServiceServicer):
    def __init__(self, manager, handle_errors):
        self.manager = manager
        self.handle_errors = handle_errors

    def _record(self, identity):
        if not re.fullmatch("[a-f0-9]{32}", identity):
            raise ValueError("job_id must be 32 lower-case hexadecimal characters")
        record = self.manager.get_record(identity)
        if not record.get("unified"):
            raise ServiceError(grpc.StatusCode.NOT_FOUND, "Unknown simulation job")
        return record

    @staticmethod
    def _job(record):
        result = record.get("result", {})
        timings = record.get("timings", {})
        state = record["state"].lower()
        if state == "cancelled":
            state = "interrupted"
        return codec.job_to_proto({
            **result, "id": record["job_id"], "state": state, "queued_at": record["created_at_unix"],
            "started_at": record.get("started_at"), "finished_at": record.get("finished_at"),
            "phase": record.get("phase", state) if state == "running" else state,
            "frames_done": record.get("frames_done", len(result.get("frames", []))),
            "frames_total": record.get("frames_total", len(result.get("frames", []))),
            "queue_seconds": timings.get("gateway_queue_seconds"),
            "execution_seconds": timings.get("total_seconds", 0) - timings.get("gateway_queue_seconds", 0),
            "error": record.get("failure", {}).get("message", ""), "artifact_manifest": record.get("artifacts", []),
        })

    def _task(self, simulation):
        validate_numbers(simulation)
        payload = codec.simulate_request_to_payload(simulation)
        for body in payload.get("bodies", []):
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
                "annotators": ["rgb", "depth_m", "semantic_ids"], "api_version": "1.0.0",
                "uptime_seconds": time.monotonic() - self.manager.started_at,
                "queue_depth": self.manager.queue.qsize(), "queue_capacity": self.manager.settings.capacity,
                "active_job": next((r["job_id"] for r in records if r["state"] == "RUNNING"), ""),
                "completed": sum(r["state"] == "SUCCEEDED" for r in records),
                "failed": sum(r["state"] == "FAILED" for r in records),
                "artifacts_root": str(root / "downloads"), "artifacts_free_bytes": shutil.disk_usage(root).free,
                "retention_seconds": self.manager.settings.retention_seconds,
            })

    async def SubmitJob(self, request, context):
        async with self.handle_errors(context):
            job = self.manager.submit_simulation(request.request_id, request.simulation,
                                                 lambda: self._task(request.simulation))
            return pb.JobHandle(id=job.job_id, state=self._job(self._record(job.job_id)).state,
                                queued_at=job.created_at_unix)

    async def GetJob(self, request, context):
        async with self.handle_errors(context):
            return self._job(self._record(request.job_id))

    async def WatchJob(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            async for _ in self.manager.observe(request.job_id):
                yield self._job(record)

    async def CancelJob(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            if record["state"] == "RUNNING":
                async with self.manager.changed:
                    self.manager._update(record, cancel_requested=True)
                    self.manager.changed.notify_all()
            else:
                await self.manager.cancel(request.job_id)
            return self._job(record)

    async def AckJob(self, request, context):
        async with self.handle_errors(context):
            record = self._record(request.job_id)
            if record["state"] != "SUCCEEDED":
                raise ServiceError(grpc.StatusCode.FAILED_PRECONDITION, "Job has not succeeded")
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
                    chunk_size = min(request.chunk_bytes or 256 * 1024, 1024 * 1024)
                    while block := await asyncio.to_thread(stream.read, chunk_size):
                        yield pb.DownloadChunk(offset=offset, data=block)
                        offset += len(block)
            finally:
                self.manager.downloads[request.job_id] -= 1
                if not self.manager.downloads[request.job_id]:
                    del self.manager.downloads[request.job_id]
