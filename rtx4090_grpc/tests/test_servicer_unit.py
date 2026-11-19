#!/usr/bin/env python3
"""RPC 层单元测试：注入假队列，不加载 Isaac Sim。

运行：
    .venv/bin/python tests/test_servicer_unit.py
"""

from __future__ import annotations

import queue
import sys
import threading
import uuid
from concurrent import futures
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import rpc  # noqa: E402
from server.servicer import SimulationServicer  # noqa: E402

pb = rpc.simulation_pb2
pb_grpc = rpc.simulation_pb2_grpc

DONE_ID = "b" * 32
MISSING_ID = "c" * 32


class FakeState:
    """与 JobQueue 的接口一致的内存实现。"""

    def __init__(self) -> None:
        self.ready = True
        self.submitted: list[dict] = []

    def health(self) -> dict:
        return {
            "status": "ready",
            "engine": "fake-engine",
            "physical_gpu": 0,
            "native_rtx": True,
            "render_backend": "native_rtx",
            "gpu_model": "Fake 4090",
            "isaac_sim": "0.0.0",
            "renderer": "RayTracedLighting",
            "supported_shapes": ["cube", "sphere", "mesh"],
            "annotators": ["rgb"],
            "uptime_seconds": 1.5,
            "queue_depth": 0,
            "queue_capacity": 64,
            "completed": 1,
            "failed": 0,
        }

    def submit(self, payload: dict) -> dict:
        # 与真实 prepare() 一致：bodies 缺省时由服务端生成默认场景
        for body in payload.get("bodies") or []:
            if body.get("shape") == "cone":
                raise ValueError("shape must be cube, sphere or mesh")
            if body.get("id") == "full":
                raise queue.Full()
        self.submitted.append(payload)
        return {"id": uuid.uuid4().hex, "state": "queued", "queued_at": 1.0}

    def status(self, job_id: str) -> dict | None:
        if job_id == DONE_ID:
            return {
                "id": job_id,
                "state": "succeeded",
                "queued_at": 1.0,
                "started_at": 1.1,
                "finished_at": 2.1,
                "bodies": [
                    {"id": "cube", "prim_path": "/World/Bodies/B0000",
                     "pose_xyzw": [0, 0, 0.1, 0, 0, 0, 1], "velocity": [0] * 6}
                ],
                "render": {"image": "/tmp/render.png", "depth_m": "/tmp/depth_m.npy"},
                "frames": [
                    {"frame": 40, "state_npz": "/tmp/frames/frame_000040.npz"},
                    {"frame": 120, "state_npz": "/tmp/frames/frame_000120.npz"},
                ],
                "state_npz": "/tmp/state.npz",
                "scene": "/tmp/scene.usdc",
                "simulation_result": "/tmp/simulation.json",
                "image": "/tmp/render.png",
            }
        return None


def expect_error(call, code) -> str:
    try:
        call()
    except grpc.RpcError as exc:
        assert exc.code() == code, f"期望 {code}，实际 {exc.code()}: {exc.details()}"
        return exc.details()
    raise AssertionError(f"期望抛出 {code}，但调用成功")


def main() -> int:
    state = FakeState()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb_grpc.add_SimulationServiceServicer_to_server(SimulationServicer(state), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()

    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.SimulationServiceStub(channel)

            health = stub.Health(pb.HealthRequest(), timeout=5)
            assert health.status == "ready" and health.gpu_model == "Fake 4090"
            assert list(health.supported_shapes) == ["cube", "sphere", "mesh"]

            request = pb.SimulateRequest()
            body = request.bodies.add(id="cube", shape="cube")
            body.position.CopyFrom(pb.Vec3(x=0, y=0, z=2))
            body.size.CopyFrom(pb.Vec3(x=0.2, y=0.2, z=0.2))
            request.steps = 10
            request.render.size = 128
            handle = stub.SubmitJob(request, timeout=5)
            assert len(handle.id) == 32 and handle.state == "queued"
            assert state.submitted[-1]["steps"] == 10
            assert state.submitted[-1]["render"]["size"] == 128
            assert state.submitted[-1]["bodies"][0]["position"] == [0.0, 0.0, 2.0]

            job = stub.GetJob(pb.GetJobRequest(job_id=DONE_ID), timeout=5)
            assert job.state == "succeeded" and job.artifacts.image == "/tmp/render.png"
            assert job.artifacts.depth_m == "/tmp/depth_m.npy"
            assert job.bodies[0].pose_xyzw[2] == 0.1
            assert [f.frame for f in job.artifacts.frames] == [40, 120]
            assert job.artifacts.frames[0].state_npz.endswith("frame_000040.npz")

            capture = pb.SimulateRequest()
            capture.bodies.add(id="cube", shape="cube")
            capture.steps = 120
            capture.capture.every = 40
            stub.SubmitJob(capture, timeout=5)
            assert state.submitted[-1]["capture"] == {"every": 40}, state.submitted[-1]

            expect_error(lambda: stub.GetJob(pb.GetJobRequest(job_id="abc"), timeout=5),
                         grpc.StatusCode.INVALID_ARGUMENT)
            expect_error(lambda: stub.GetJob(pb.GetJobRequest(job_id=MISSING_ID), timeout=5),
                         grpc.StatusCode.NOT_FOUND)

            empty = pb.SimulateRequest()
            handle2 = stub.SubmitJob(empty, timeout=5)
            assert len(handle2.id) == 32
            assert "bodies" not in state.submitted[-1], "缺省 bodies 不应写入 payload"

            invalid = pb.SimulateRequest()
            invalid.bodies.add(id="bad", shape="cone")
            expect_error(lambda: stub.SubmitJob(invalid, timeout=5), grpc.StatusCode.INVALID_ARGUMENT)

            full = pb.SimulateRequest()
            full.bodies.add(id="full")
            expect_error(lambda: stub.SubmitJob(full, timeout=5), grpc.StatusCode.RESOURCE_EXHAUSTED)
    finally:
        server.stop(grace=0).wait()

    print("PASS: RPC 层单元测试通过（Health / SubmitJob / GetJob + 错误码映射）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
