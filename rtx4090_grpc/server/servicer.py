"""SimulationService 实现：通过 JobManager 暴露完整 gRPC 契约。"""

from __future__ import annotations

import asyncio
import re
import os
from typing import Any

import grpc

from . import codec, job_manager, rpc

pb = rpc.simulation_pb2
pb_grpc = rpc.simulation_pb2_grpc

JOB_ID = re.compile(r"^[a-f0-9]{32}$")


def _valid_job_id(job_id: str, context) -> str:
    job_id = (job_id or "").strip()
    if not JOB_ID.fullmatch(job_id):
        context.abort(grpc.StatusCode.INVALID_ARGUMENT,
                      "job_id 必须是 32 位小写十六进制字符串")
    return job_id


class SimulationServicer(pb_grpc.SimulationServiceServicer):
    """把 JobManager 映射成 gRPC 方法。"""

    def __init__(self, manager: job_manager.JobManager) -> None:
        self._manager = manager

    # ------------------------------------------------------------------
    def Health(self, request: pb.HealthRequest, context) -> pb.HealthResponse:
        try:
            record = self._manager.health()
        except Exception as exc:  # noqa: BLE001
            context.abort(grpc.StatusCode.INTERNAL, f"健康检查失败: {exc}")
        return codec.health_to_proto(record)

    # ------------------------------------------------------------------
    def GetCapabilities(self, request, context):
        from .retained_api import capabilities

        return capabilities(self._manager.health())

    def GetApiCatalog(self, request, context):
        from .retained_api import catalog

        return catalog(self._manager.health())

    def _assets(self):
        from a100sim.rpc.storage import AssetStore
        from a100sim.service_queue import ASSET_ROOT
        from .rpc import native_pb2

        return AssetStore(ASSET_ROOT, int(os.environ.get("RTX4090_MAX_UPLOAD_BYTES", 512 * 1024**2)),
                          messages=native_pb2)

    def ListAssets(self, request, context):
        return self._assets().list_assets()

    def UploadAsset(self, requests, context):
        from a100sim.rpc.errors import ServiceError

        async def chunks():
            for chunk in requests:
                yield chunk
        try:
            return asyncio.run(self._assets().upload(chunks()))
        except ServiceError as exc:
            context.abort(exc.code, str(exc))
        except (ValueError, TypeError) as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))

    def SubmitJob(self, request: pb.SubmitJobRequest, context) -> pb.JobHandle:
        try:
            field = request.WhichOneof("workload")
            if field == "simulation":
                payload = codec.simulate_request_to_payload(request.simulation)
            elif field in ("native_task", "native_feature"):
                from .retained_api import native_payload
                payload = native_payload(request)
            else:
                raise ValueError("SubmitJob requires a simulation or native workload")
            record = self._manager.submit(request.request_id, payload)
        except job_manager.IdempotencyConflict as exc:
            context.abort(grpc.StatusCode.ALREADY_EXISTS, str(exc))
        except job_manager.DiskFull as exc:
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
        except Exception as exc:  # 上游队列的 queue.Full / RuntimeError / ValueError 等
            self._abort_submit(exc, context)
        return codec.job_handle_to_proto(record)

    @staticmethod
    def _abort_submit(exc: Exception, context) -> None:
        import queue as _q

        if isinstance(exc, _q.Full):
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "GPU 队列已满，任务未提交")
        if isinstance(exc, RuntimeError):
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        if isinstance(exc, (ValueError, TypeError, KeyError)):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        context.abort(grpc.StatusCode.INTERNAL, f"提交失败: {exc}")

    # ------------------------------------------------------------------
    def GetJob(self, request: pb.GetJobRequest, context) -> pb.Job:
        job_id = _valid_job_id(request.job_id, context)
        record = self._manager.status(job_id)
        if record is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"任务不存在: {job_id}")
        return codec.job_to_proto(record)

    # ------------------------------------------------------------------
    def DownloadArtifact(self, request: pb.DownloadArtifactRequest, context):
        try:
            handle = self._manager.download_open(
                request.job_id, request.artifact_id, request.offset, request.chunk_bytes
            )
        except job_manager.ArtifactReleased as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except job_manager.ArtifactNotFound as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        except job_manager.JobNotReady as exc:
            context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
        except job_manager.OutOfRange as exc:
            context.abort(grpc.StatusCode.OUT_OF_RANGE, str(exc))
        except job_manager.TooManyStreams as exc:
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(exc))
        for offset, data in self._manager.stream_chunks(handle):
            yield pb.DownloadChunk(offset=offset, data=data)

    # ------------------------------------------------------------------
    def AckJob(self, request: pb.AckJobRequest, context) -> pb.AckJobResponse:
        job_id = _valid_job_id(request.job_id, context)
        try:
            result = self._manager.ack(job_id)
        except job_manager.ArtifactNotFound as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
        return pb.AckJobResponse(
            released=bool(result["released"]),
            freed_bytes=int(result["freed_bytes"]),
        )

    # ------------------------------------------------------------------
    def CancelJob(self, request: pb.GetJobRequest, context) -> pb.Job:
        job_id = _valid_job_id(request.job_id, context)
        before = self._manager.status(job_id)
        if before and before.get("native_operation"):
            with self._manager.lock:
                if self._manager.status(job_id)["state"] == "running":
                    context.abort(grpc.StatusCode.FAILED_PRECONDITION, "The retained operation is already running")
                record = self._manager.cancel(job_id)
        else:
            record = self._manager.cancel(job_id)
        if record is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"任务不存在: {job_id}")
        return codec.job_to_proto(record)

    # ------------------------------------------------------------------
    def WatchJob(self, request: pb.GetJobRequest, context):
        job_id = _valid_job_id(request.job_id, context)
        if self._manager.status(job_id) is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"任务不存在: {job_id}")
        for record in self._manager.watch_iter(job_id, context.is_active):
            message = codec.job_to_proto(record)
            if not message.HasField("native_job") or message.native_job.revision > request.after_revision:
                yield message
