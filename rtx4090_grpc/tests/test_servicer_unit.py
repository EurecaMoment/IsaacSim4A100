#!/usr/bin/env python3
"""RPC 层单元测试：注入假 manager，不加载 Isaac Sim。

运行：
    .venv/bin/python tests/test_servicer_unit.py
"""

from __future__ import annotations

import queue
import sys
import uuid
from concurrent import futures
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import rpc  # noqa: E402
from server import job_manager  # noqa: E402
from server.servicer import SimulationServicer  # noqa: E402

pb = rpc.simulation_pb2
pb_grpc = rpc.simulation_pb2_grpc

DONE_ID = "b" * 32
MISSING_ID = "c" * 32
RUNNING_ID = "d" * 32


class FakeManager:
    """模仿 JobManager 接口的内存实现。"""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict]] = []

    def health(self) -> dict:
        return {
            "status": "ready", "engine": "fake-engine", "physical_gpu": 0,
            "native_rtx": True, "render_backend": "native_rtx", "gpu_model": "Fake 4090",
            "isaac_sim": "0.0.0", "renderer": "RayTracedLighting",
            "supported_shapes": ["cube", "sphere", "mesh"], "annotators": ["rgb"],
            "uptime_seconds": 1.5, "queue_depth": 0, "queue_capacity": 64,
            "completed": 1, "failed": 0, "api_version": "1.0.0",
            "artifacts_root": "/tmp/jobs", "artifacts_free_bytes": 1024, "retention_seconds": 604800,
        }

    def submit(self, request_id: str, payload: dict) -> dict:
        if not request_id.strip():
            raise ValueError("request_id 不能为空")
        if request_id == "dup":
            raise job_manager.IdempotencyConflict("aaaa" + "0" * 28)
        for body in payload.get("bodies") or []:
            if body.get("shape") == "cone":
                raise ValueError("shape must be cube, sphere or mesh")
            if body.get("id") == "full":
                raise queue.Full()
        self.submitted.append((request_id, payload))
        return {"id": uuid.uuid4().hex, "state": "queued", "queued_at": 1.0}

    def status(self, job_id: str) -> dict | None:
        if job_id == DONE_ID:
            return {
                "id": job_id, "state": "succeeded", "queued_at": 1.0,
                "started_at": 1.1, "finished_at": 2.1, "execution_seconds": 1.0,
                "phase": "writing_artifacts", "frames_done": 2, "frames_total": 2,
                "bodies": [{"id": "cube", "prim_path": "/World/Bodies/B0000",
                            "pose_xyzw": [0, 0, 0.1, 0, 0, 0, 1], "velocity": [0] * 6}],
                "render": {"image": "/tmp/render.png", "depth_m": "/tmp/depth_m.npy",
                           "native_rtx": True, "size": 128, "renderer": "RayTracedLighting",
                           "requested_spp": 8},
                "image": "/tmp/render.png", "state_npz": "/tmp/state.npz",
                "scene": "/tmp/scene.usdc", "simulation_result": "/tmp/simulation.json",
                "artifact_manifest": [
                    {"artifact_id": "aid1", "name": "render.png",
                     "relative_path": f"{DONE_ID}/render.png", "size_bytes": 10,
                     "sha256": "a" * 64, "media_type": "image/png"},
                    {"artifact_id": "aid2", "name": "state.npz",
                     "relative_path": f"{DONE_ID}/state.npz", "size_bytes": 20,
                     "sha256": "b" * 64, "media_type": "application/x-npz"},
                ],
            }
        if job_id == RUNNING_ID:
            return {"id": job_id, "state": "running", "phase": "simulating",
                    "frames_done": 3, "frames_total": 10}
        return None

    def download_open(self, job_id, artifact_id, offset, chunk_bytes):
        if job_id == MISSING_ID or artifact_id == "nope":
            raise job_manager.ArtifactNotFound("not found")
        if artifact_id == "released":
            raise job_manager.ArtifactReleased("artifacts_released")
        return {"job_id": job_id, "size": 5, "offset": 0, "chunk_bytes": chunk_bytes,
                "_data": b"HELLO"}

    def stream_chunks(self, handle):
        data = handle["_data"]
        off = 0
        while off < len(data):
            yield off, data[off:off + 2]
            off += 2

    def ack(self, job_id):
        return {"released": True, "freed_bytes": 30}

    def cancel(self, job_id):
        if job_id == MISSING_ID:
            return None
        return {"id": job_id, "state": "interrupted", "error": "cancelled"}

    def watch_iter(self, job_id, is_active):
        yield {"id": job_id, "state": "succeeded"}
        return
        yield  # noqa: unreachable


def expect_error(call, code) -> str:
    try:
        call()
    except grpc.RpcError as exc:
        assert exc.code() == code, f"期望 {code}，实际 {exc.code()}: {exc.details()}"
        return exc.details()
    raise AssertionError(f"期望抛出 {code}，但调用成功")


def _submit(simulation, request_id="req-1"):
    return pb.SubmitJobRequest(request_id=request_id, simulation=simulation)


def main() -> int:
    manager = FakeManager()
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb_grpc.add_SimulationServiceServicer_to_server(SimulationServicer(manager), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()

    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = pb_grpc.SimulationServiceStub(channel)

            # Health：新字段
            health = stub.Health(pb.HealthRequest(), timeout=5)
            assert health.status == "ready"
            assert health.api_version == "1.0.0"
            assert health.artifacts_free_bytes == 1024
            assert health.retention_seconds == 604800

            # SubmitJob：request_id + simulation 透传
            sim = pb.SimulateRequest()
            body = sim.bodies.add(id="cube", shape="cube")
            body.position.CopyFrom(pb.Vec3(x=0, y=0, z=2))
            body.size.CopyFrom(pb.Vec3(x=0.2, y=0.2, z=0.2))
            sim.steps = 10
            sim.render.size = 128
            handle = stub.SubmitJob(_submit(sim), timeout=5)
            assert len(handle.id) == 32 and handle.state == "queued"
            rid, payload = manager.submitted[-1]
            assert rid == "req-1"
            assert payload["steps"] == 10
            assert payload["render"]["size"] == 128
            assert payload["bodies"][0]["position"] == [0.0, 0.0, 2.0]

            # 幂等冲突 → ALREADY_EXISTS
            expect_error(lambda: stub.SubmitJob(_submit(sim, request_id="dup"), timeout=5),
                         grpc.StatusCode.ALREADY_EXISTS)
            # 空 request_id → INVALID_ARGUMENT
            expect_error(lambda: stub.SubmitJob(_submit(sim, request_id=""), timeout=5),
                         grpc.StatusCode.INVALID_ARGUMENT)
            # 非法 shape → INVALID_ARGUMENT
            bad = pb.SimulateRequest()
            bad.bodies.add(id="bad", shape="cone")
            expect_error(lambda: stub.SubmitJob(_submit(bad), timeout=5),
                         grpc.StatusCode.INVALID_ARGUMENT)
            # 队列满 → RESOURCE_EXHAUSTED
            full = pb.SimulateRequest()
            full.bodies.add(id="full")
            expect_error(lambda: stub.SubmitJob(_submit(full), timeout=5),
                         grpc.StatusCode.RESOURCE_EXHAUSTED)

            # GetJob：产物清单 + 遗留 outputs + 进度
            job = stub.GetJob(pb.GetJobRequest(job_id=DONE_ID), timeout=5)
            assert job.state == "succeeded"
            assert job.phase == "writing_artifacts"
            assert job.frames_done == 2 and job.frames_total == 2
            assert len(job.artifacts) == 2
            assert job.artifacts[0].artifact_id == "aid1"
            assert job.artifacts[0].size_bytes == 10
            assert job.artifacts[0].sha256 == "a" * 64
            assert job.artifacts[1].relative_path.endswith("/state.npz")
            # 遗留绝对路径字段改名为 outputs
            assert job.outputs.image == "/tmp/render.png"
            assert job.outputs.scene == "/tmp/scene.usdc"
            assert job.render.native_rtx is True

            # 运行中任务返回进度
            running = stub.GetJob(pb.GetJobRequest(job_id=RUNNING_ID), timeout=5)
            assert running.phase == "simulating" and running.frames_done == 3

            # GetJob 错误码
            expect_error(lambda: stub.GetJob(pb.GetJobRequest(job_id="abc"), timeout=5),
                         grpc.StatusCode.INVALID_ARGUMENT)
            expect_error(lambda: stub.GetJob(pb.GetJobRequest(job_id=MISSING_ID), timeout=5),
                         grpc.StatusCode.NOT_FOUND)

            # DownloadArtifact：流式
            chunks = list(stub.DownloadArtifact(
                pb.DownloadArtifactRequest(job_id=DONE_ID, artifact_id="aid1", offset=0, chunk_bytes=2),
                timeout=5))
            assert b"".join(c.data for c in chunks) == b"HELLO"
            assert chunks[0].offset == 0
            # 错误路径
            expect_error(lambda: list(stub.DownloadArtifact(
                pb.DownloadArtifactRequest(job_id=DONE_ID, artifact_id="nope"), timeout=5)),
                grpc.StatusCode.NOT_FOUND)
            expect_error(lambda: list(stub.DownloadArtifact(
                pb.DownloadArtifactRequest(job_id=DONE_ID, artifact_id="released"), timeout=5)),
                grpc.StatusCode.NOT_FOUND)

            # AckJob
            ack = stub.AckJob(pb.AckJobRequest(job_id=DONE_ID), timeout=5)
            assert ack.released is True and ack.freed_bytes == 30

            # CancelJob
            cancelled = stub.CancelJob(pb.GetJobRequest(job_id=DONE_ID), timeout=5)
            assert cancelled.state == "interrupted"
            expect_error(lambda: stub.CancelJob(pb.GetJobRequest(job_id=MISSING_ID), timeout=5),
                         grpc.StatusCode.NOT_FOUND)

            # WatchJob
            watches = list(stub.WatchJob(pb.GetJobRequest(job_id=DONE_ID), timeout=5))
            assert watches and watches[0].state == "succeeded"

    finally:
        server.stop(grace=0).wait()

    print("PASS: RPC 层单元测试通过（新契约全部 RPC + 错误码映射）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
