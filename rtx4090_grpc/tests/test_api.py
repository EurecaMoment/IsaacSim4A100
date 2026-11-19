#!/usr/bin/env python3
"""针对已启动的 gRPC 服务做接口级测试。

覆盖：
  1. Health      —— 元信息、就绪状态、队列/渲染后端
  2. SubmitJob   —— 提交成功、job_id 格式、初始状态
  3. GetJob      —— 提交后立即可查、轮询至完成
  4. 结果完整性  —— 刚体、渲染元信息、产物文件是否落盘
  5. 错误码      —— 非法 job_id / 任务不存在 / 非法 shape

用法：
    bash scripts/test/test_api.sh
    .venv/bin/python tests/test_api.py --target 127.0.0.1:18500
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

MAX_MESSAGE = 32 * 1024 * 1024


class Report:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []

    def run(self, name: str, func) -> bool:
        try:
            detail = func()
        except AssertionError as exc:
            self.failed.append(f"{name}: {exc}")
            print(f"[FAIL] {name}: {exc}")
            return False
        except grpc.RpcError as exc:
            self.failed.append(f"{name}: RPC {exc.code().name}: {exc.details()}")
            print(f"[FAIL] {name}: RPC {exc.code().name}: {exc.details()}")
            return False
        self.passed.append(name)
        print(f"[PASS] {name}" + (f" -> {detail}" if detail else ""))
        return True

    def summary(self) -> int:
        print(f"\n通过 {len(self.passed)} 项，失败 {len(self.failed)} 项")
        for item in self.failed:
            print(f"  - {item}")
        return 0 if not self.failed else 1


def expect_code(call, code) -> None:
    try:
        call()
    except grpc.RpcError as exc:
        assert exc.code() == code, f"期望 {code}，实际 {exc.code()}: {exc.details()}"
        return
    raise AssertionError(f"期望抛出 {code}，但调用成功")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default="127.0.0.1:18500")
    parser.add_argument("--scene", default="cube")
    parser.add_argument("--timeout", type=float, default=300.0, help="等待任务完成的秒数")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--capture-every", type=int, default=0,
                        help="每 N 帧采样一次状态；0 表示不采样（只保留尾帧）")
    args = parser.parse_args()

    report = Report()
    options = (
        ("grpc.max_send_message_length", MAX_MESSAGE),
        ("grpc.max_receive_message_length", MAX_MESSAGE),
    )

    with grpc.insecure_channel(args.target, options=options) as channel:
        stub = pb_grpc.SimulationServiceStub(channel)
        print(f"目标服务：{args.target}\n")

        state: dict = {}

        def check_health():
            health = stub.Health(pb.HealthRequest(), timeout=15)
            state["health"] = health
            require(bool(health.status), "status 为空")
            require(bool(health.engine), "engine 为空")
            require(bool(health.isaac_sim), "isaac_sim 为空")
            return f"status={health.status} gpu={health.gpu_model} sim={health.isaac_sim}"

        if not report.run("Health：返回运行时元信息", check_health):
            print("\n提示：服务未启动或不可达，请先执行 bash start.sh", file=sys.stderr)
            return report.summary()

        def check_ready():
            health = state["health"]
            require(health.status == "ready", f"status={health.status}，引擎尚未就绪")
            return "ready"

        report.run("Health：服务就绪", check_ready)

        def check_capabilities():
            health = state["health"]
            require(bool(health.render_backend), "render_backend 为空")
            require(health.queue_capacity > 0, "queue_capacity 异常")
            require("cube" in health.supported_shapes, "supported_shapes 缺少 cube")
            return (
                f"backend={health.render_backend} "
                f"queue={health.queue_depth}/{health.queue_capacity} "
                f"shapes={list(health.supported_shapes)}"
            )

        report.run("Health：能力与队列信息", check_capabilities)

        def check_submit():
            request = scenes.load(args.scene)
            if args.capture_every:
                request.capture.every = args.capture_every
            state["steps"] = request.steps
            handle = stub.SubmitJob(request, timeout=30)
            state["handle"] = handle
            require(len(handle.id) == 32, f"job_id 长度异常: {handle.id!r}")
            int(handle.id, 16)  # 必须是十六进制
            require(handle.id == handle.id.lower(), "job_id 必须小写")
            require(handle.state == "queued", f"初始状态应为 queued，实际 {handle.state}")
            return f"id={handle.id} state={handle.state}"

        if not report.run("SubmitJob：提交任务并返回 job 句柄", check_submit):
            return report.summary()

        def check_immediate():
            job = stub.GetJob(pb.GetJobRequest(job_id=state["handle"].id), timeout=15)
            require(
                job.state in {"queued", "running", "succeeded"},
                f"状态异常: {job.state}",
            )
            return f"state={job.state}"

        report.run("GetJob：提交后立即可查询", check_immediate)

        def wait_done():
            deadline = time.monotonic() + args.timeout
            job = None
            while time.monotonic() < deadline:
                job = stub.GetJob(pb.GetJobRequest(job_id=state["handle"].id), timeout=30)
                if job.state not in {"queued", "running"}:
                    state["job"] = job
                    return f"state={job.state} 执行耗时 {job.execution_seconds:.2f}s"
                time.sleep(args.interval)
            raise AssertionError(
                f"等待超时（{args.timeout}s），最后状态 {job.state if job else '未知'}"
            )

        if not report.run("GetJob：轮询直到任务结束", wait_done):
            return report.summary()

        def check_succeeded():
            job = state["job"]
            require(job.state == "succeeded", f"state={job.state} error={job.error}")
            return "succeeded"

        report.run("GetJob：任务成功", check_succeeded)

        def check_bodies():
            job = state["job"]
            require(len(job.bodies) == 2, f"bodies={len(job.bodies)}，期望 2")
            require(all(len(b.pose_xyzw) == 7 for b in job.bodies), "pose_xyzw 维度应为 7")
            require(all(len(b.velocity) == 6 for b in job.bodies), "velocity 维度应为 6")
            return f"{len(job.bodies)} 个刚体，pose 7 维 / velocity 6 维"

        report.run("GetJob：刚体状态完整", check_bodies)

        def check_render():
            render = state["job"].render
            require(render.native_rtx, "native_rtx 应为 true")
            require(render.size > 0, "render.size 缺失")
            require(bool(render.renderer), "renderer 缺失")
            return f"{render.size}px spp={render.requested_spp} renderer={render.renderer}"

        report.run("GetJob：渲染元信息完整", check_render)

        def check_artifacts():
            artifacts = state["job"].artifacts
            paths = {
                "image": artifacts.image,
                "depth_m": artifacts.depth_m,
                "semantic_ids": artifacts.semantic_ids,
                "scene": artifacts.scene,
                "simulation_json": artifacts.simulation_json,
            }
            missing = [name for name, path in paths.items() if not path]
            require(not missing, f"缺少产物路径: {missing}")
            absent = [name for name, path in paths.items() if not Path(path).is_file()]
            require(not absent, f"文件不存在: {absent}")
            return f"{len(paths)} 个产物齐备，目录 {Path(paths['image']).parent}"

        report.run("产物：路径有效且文件已落盘", check_artifacts)

        def check_frames():
            frames = list(state["job"].artifacts.frames)
            require(bool(frames), "未返回按帧采样结果")
            require(all(Path(frame.state_npz).is_file() for frame in frames), "帧状态文件缺失")
            got = [frame.frame for frame in frames]
            steps = state.get("steps") or 0
            if steps:
                expected = sorted(set(range(args.capture_every, steps + 1, args.capture_every)) | {steps})
                require(got == expected, f"帧号 {got} != 期望 {expected}")
            return f"{len(frames)} 帧：{got}"

        if args.capture_every:
            report.run("GetJob：按帧采样结果", check_frames)

        def bad_job_id():
            expect_code(
                lambda: stub.GetJob(pb.GetJobRequest(job_id="abc"), timeout=10),
                grpc.StatusCode.INVALID_ARGUMENT,
            )
            return "INVALID_ARGUMENT"

        report.run("错误码：非法 job_id", bad_job_id)

        def missing_job():
            expect_code(
                lambda: stub.GetJob(pb.GetJobRequest(job_id="0" * 32), timeout=10),
                grpc.StatusCode.NOT_FOUND,
            )
            return "NOT_FOUND"

        report.run("错误码：任务不存在", missing_job)

        def bad_shape():
            bad = pb.SimulateRequest()
            bad.bodies.add(id="bad", shape="cone")
            expect_code(lambda: stub.SubmitJob(bad, timeout=10), grpc.StatusCode.INVALID_ARGUMENT)
            return "INVALID_ARGUMENT"

        report.run("错误码：非法几何体类型", bad_shape)

    return report.summary()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except grpc.RpcError as exc:
        print(f"\n无法连接服务：{exc.code().name}: {exc.details()}", file=sys.stderr)
        print("请先启动：bash start.sh", file=sys.stderr)
        raise SystemExit(2)
