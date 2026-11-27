#!/usr/bin/env python3
"""针对运行中的 gRPC 服务做端到端冒烟测试。

用法：
    .venv/bin/python tests/smoke_test.py --target 127.0.0.1:18500
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import uuid
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from client import scenes  # noqa: E402
from server import rpc  # noqa: E402

pb = rpc.simulation_pb2
pb_grpc = rpc.simulation_pb2_grpc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="127.0.0.1:18500")
    parser.add_argument("--scene", default="cube")
    parser.add_argument("--wait-timeout", type=float, default=180.0)
    args = parser.parse_args()

    with grpc.insecure_channel(args.target) as channel:
        stub = pb_grpc.SimulationServiceStub(channel)

        health = stub.Health(pb.HealthRequest(), timeout=10)
        print(f"[1/3] Health: status={health.status} engine={health.engine} gpu={health.gpu_model}")
        if health.status != "ready":
            print("FAIL: 上游服务未就绪")
            return 1

        request_id = uuid.uuid4().hex
        submit_req = pb.SubmitJobRequest(request_id=request_id, simulation=scenes.load(args.scene))
        handle = stub.SubmitJob(submit_req, timeout=30)
        print(f"[2/4] SubmitJob: id={handle.id} state={handle.state} request_id={request_id}")
        if len(handle.id) != 32:
            print("FAIL: job_id 格式异常")
            return 1
        # 幂等：同 request_id 再提交应返回同一 job_id
        handle2 = stub.SubmitJob(submit_req, timeout=30)
        if handle2.id != handle.id:
            print("FAIL: 幂等命中未返回同一 job_id")
            return 1

        deadline = time.monotonic() + args.wait_timeout
        job = None
        while time.monotonic() < deadline:
            job = stub.GetJob(pb.GetJobRequest(job_id=handle.id), timeout=30)
            if job.state not in {"queued", "running"}:
                break
            time.sleep(0.5)

        if job is None or job.state == "":
            print("FAIL: 轮询超时，任务未完成")
            return 1
        print(f"[3/4] GetJob: state={job.state} bodies={len(job.bodies)} artifacts={len(job.artifacts)}")
        if job.state != "succeeded":
            print(f"FAIL: 任务失败 error={job.error}")
            return 1

        print("产物：")
        for name, value in (
            ("render.png", job.outputs.image),
            ("depth_m.npy", job.outputs.depth_m),
            ("semantic_ids.npy", job.outputs.semantic_ids),
            ("scene.usdc", job.outputs.scene),
            ("simulation.json", job.outputs.simulation_json),
        ):
            print(f"  {name}: {value or '(缺失)'}")
        if not job.outputs.image:
            print("FAIL: 缺少渲染产物路径")
            return 1
        if not job.artifacts:
            print("FAIL: 产物清单为空")
            return 1

        # 下载尾帧并校验 sha256，然后 Ack
        render = next((a for a in job.artifacts if a.name == "render.png"), None)
        if render is None:
            print("FAIL: 清单中无 render.png")
            return 1
        hasher = hashlib.sha256()
        total = 0
        for chunk in stub.DownloadArtifact(
            pb.DownloadArtifactRequest(job_id=handle.id, artifact_id=render.artifact_id), timeout=60):
            hasher.update(chunk.data)
            total += len(chunk.data)
        if total != render.size_bytes:
            print(f"FAIL: 下载字节数 {total} != 清单 {render.size_bytes}")
            return 1
        if hasher.hexdigest() != render.sha256:
            print("FAIL: sha256 校验失败")
            return 1
        ack = stub.AckJob(pb.AckJobRequest(job_id=handle.id), timeout=30)
        print(f"[4/4] Download+Verify+Ack: {total}B sha256 OK, freed={ack.freed_bytes}B released={ack.released}")

    print("\nPASS: gRPC 端到端冒烟通过（Health/Submit/Get/Download/Ack + 幂等）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
