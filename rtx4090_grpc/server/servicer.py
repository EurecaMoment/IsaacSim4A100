"""SimulationService 实现：直接操作本地作业队列（与 HTTP 版平级）。"""

from __future__ import annotations

import queue as queue_module
import re
from typing import Any

import grpc

from . import codec, rpc

pb = rpc.simulation_pb2
pb_grpc = rpc.simulation_pb2_grpc

JOB_ID = re.compile(r"^[a-f0-9]{32}$")


class SimulationServicer(pb_grpc.SimulationServiceServicer):
    """把 HTTP 版的三个接口等价映射成 gRPC 方法。

    参数：JobQueue 实例（RtxQueue），与 HTTP 版共用同一实现。
    """

    def __init__(self, state: Any) -> None:
        self._state = state

    def Health(self, request: pb.HealthRequest, context) -> pb.HealthResponse:
        try:
            record = self._state.health()
        except Exception as exc:  # 健康检查不应因异常中断连接
            context.abort(grpc.StatusCode.INTERNAL, f"健康检查失败: {exc}")
        return codec.health_to_proto(record)

    def SubmitJob(self, request: pb.SimulateRequest, context) -> pb.JobHandle:
        payload = codec.simulate_request_to_payload(request)
        try:
            record = self._state.submit(payload)
        except queue_module.Full:
            context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "GPU 队列已满，任务未提交")
        except RuntimeError as exc:
            context.abort(grpc.StatusCode.UNAVAILABLE, str(exc))
        except (ValueError, TypeError, KeyError) as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        return codec.job_handle_to_proto(record)

    def GetJob(self, request: pb.GetJobRequest, context) -> pb.Job:
        job_id = request.job_id.strip()
        if not JOB_ID.fullmatch(job_id):
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "job_id 必须是 32 位小写十六进制字符串")
        try:
            record = self._state.status(job_id)
        except ValueError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        if record is None:
            context.abort(grpc.StatusCode.NOT_FOUND, f"任务不存在: {job_id}")
        return codec.job_to_proto(record)
