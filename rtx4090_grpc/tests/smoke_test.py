#!/usr/bin/env python3
"""针对运行中的 gRPC 服务做端到端冒烟测试。

用法：
    .venv/bin/python tests/smoke_test.py --target 127.0.0.1:18500
"""

from __future__ import annotations

import argparse
import sys
import time
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

        handle = stub.SubmitJob(scenes.load(args.scene), timeout=30)
        print(f"[2/3] SubmitJob: id={handle.id} state={handle.state}")
        if len(handle.id) != 32:
            print("FAIL: job_id 格式异常")
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
        print(f"[3/3] GetJob: state={job.state} bodies={len(job.bodies)}")
        if job.state != "succeeded":
            print(f"FAIL: 任务失败 error={job.error}")
            return 1

        print("产物：")
        for name, value in (
            ("render.png", job.artifacts.image),
            ("depth_m.npy", job.artifacts.depth_m),
            ("semantic_ids.npy", job.artifacts.semantic_ids),
            ("scene.usdc", job.artifacts.scene),
            ("simulation.json", job.artifacts.simulation_json),
        ):
            print(f"  {name}: {value or '(缺失)'}")
        if not job.artifacts.image:
            print("FAIL: 缺少渲染产物路径")
            return 1

    print("\nPASS: gRPC 三个接口端到端验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
