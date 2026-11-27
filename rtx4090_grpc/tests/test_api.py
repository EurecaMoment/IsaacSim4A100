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

        def check_health_contract():
            health = state["health"]
            require(bool(health.api_version), "api_version 为空")
            require(bool(health.artifacts_root), "artifacts_root 为空")
            require(health.retention_seconds > 0, "retention_seconds 异常")
            require(health.artifacts_free_bytes >= 0, "artifacts_free_bytes 异常")
            return (f"api={health.api_version} root={health.artifacts_root} "
                    f"free={health.artifacts_free_bytes} retention={health.retention_seconds}")

        report.run("Health：契约版本与产物元信息", check_health_contract)

        def check_submit():
            request = scenes.load(args.scene)
            if args.capture_every:
                request.capture.every = args.capture_every
            state["steps"] = request.steps
            state["request_id"] = uuid.uuid4().hex
            state["simulation"] = request
            submit_req = pb.SubmitJobRequest(request_id=state["request_id"], simulation=request)
            handle = stub.SubmitJob(submit_req, timeout=30)
            state["handle"] = handle
            require(len(handle.id) == 32, f"job_id 长度异常: {handle.id!r}")
            int(handle.id, 16)  # 必须是十六进制
            require(handle.id == handle.id.lower(), "job_id 必须小写")
            require(handle.state == "queued", f"初始状态应为 queued，实际 {handle.state}")
            return f"id={handle.id} state={handle.state}"

        if not report.run("SubmitJob：提交任务并返回 job 句柄", check_submit):
            return report.summary()

        def check_idempotent():
            # 同 request_id + 同参数 → 幂等返回同一 job_id
            submit_req = pb.SubmitJobRequest(
                request_id=state["request_id"], simulation=state["simulation"])
            handle2 = stub.SubmitJob(submit_req, timeout=30)
            require(handle2.id == state["handle"].id,
                    f"幂等命中应返回同一 job_id，期望 {state['handle'].id} 实际 {handle2.id}")
            return f"幂等返回同一 job_id={handle2.id}"

        report.run("SubmitJob：同 request_id 幂等返回", check_idempotent)

        def check_idempotent_conflict():
            different = pb.SimulateRequest(steps=999)
            submit_req = pb.SubmitJobRequest(request_id=state["request_id"], simulation=different)
            expect_code(lambda: stub.SubmitJob(submit_req, timeout=10),
                        grpc.StatusCode.ALREADY_EXISTS)
            return "ALREADY_EXISTS"

        report.run("SubmitJob：同 request_id 不同参数 → ALREADY_EXISTS", check_idempotent_conflict)

        def check_empty_request_id():
            submit_req = pb.SubmitJobRequest(request_id="", simulation=state["simulation"])
            expect_code(lambda: stub.SubmitJob(submit_req, timeout=10),
                        grpc.StatusCode.INVALID_ARGUMENT)
            return "INVALID_ARGUMENT"

        report.run("SubmitJob：空 request_id → INVALID_ARGUMENT", check_empty_request_id)

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
            outputs = state["job"].outputs
            paths = {
                "image": outputs.image,
                "depth_m": outputs.depth_m,
                "semantic_ids": outputs.semantic_ids,
                "scene": outputs.scene,
                "simulation_json": outputs.simulation_json,
            }
            missing = [name for name, path in paths.items() if not path]
            require(not missing, f"缺少产物路径: {missing}")
            absent = [name for name, path in paths.items() if not Path(path).is_file()]
            require(not absent, f"文件不存在: {absent}")
            return f"{len(paths)} 个产物齐备，目录 {Path(paths['image']).parent}"

        report.run("产物：路径有效且文件已落盘", check_artifacts)

        def check_manifest():
            job = state["job"]
            require(len(job.artifacts) > 0, "产物清单为空")
            by_rel = {a.relative_path: a for a in job.artifacts}
            # 顶层产物必须在清单内
            require(job.outputs.image, "缺少 outputs.image")
            image_rel = f"{job.id}/render.png"
            require(image_rel in by_rel, f"清单缺少 {image_rel}")
            entry = by_rel[image_rel]
            require(entry.size_bytes > 0, "size_bytes 缺失")
            require(len(entry.sha256) == 64, "sha256 应为 64 位 hex")
            require(entry.media_type == "image/png", f"media_type 异常: {entry.media_type}")
            require(entry.artifact_id, "artifact_id 为空")
            # artifact_id 在任务内唯一
            ids = [a.artifact_id for a in job.artifacts]
            require(len(ids) == len(set(ids)), "artifact_id 不唯一")
            return f"{len(job.artifacts)} 个产物，首项 {ids[0]}"

        report.run("GetJob：产物清单（artifact_id/size/sha256/media_type）", check_manifest)

        def check_frames():
            frames = list(state["job"].outputs.frames)
            require(bool(frames), "未返回按帧采样结果")
            require(all(Path(frame.state_npz).is_file() for frame in frames), "帧状态文件缺失")
            got = [frame.frame for frame in frames]
            steps = state.get("steps") or 0
            if steps:
                expected = sorted(set(range(args.capture_every, steps + 1, args.capture_every)) | {steps})
                require(got == expected, f"帧号 {got} != 期望 {expected}")
            # 帧产物也应出现在 manifest 中
            by_rel = {a.relative_path for a in state["job"].artifacts}
            for frame in frames:
                rel = f"{state['job'].id}/frames/frame_{frame.frame:06d}/state.npz"
                require(rel in by_rel, f"帧产物未在 manifest: {rel}")
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
            submit_req = pb.SubmitJobRequest(request_id=uuid.uuid4().hex, simulation=bad)
            expect_code(lambda: stub.SubmitJob(submit_req, timeout=10),
                        grpc.StatusCode.INVALID_ARGUMENT)
            return "INVALID_ARGUMENT"

        report.run("错误码：非法几何体类型", bad_shape)

        def check_download():
            job = state["job"]
            # 选 outputs.image 对应的顶层 render.png（capture 场景下避免取到帧 render.png）
            image_rel = f"{job.id}/render.png"
            target = next((a for a in job.artifacts if a.relative_path == image_rel), None)
            require(target is not None, f"manifest 中无顶层 {image_rel}")
            hasher = hashlib.sha256()
            total = 0
            for chunk in stub.DownloadArtifact(
                pb.DownloadArtifactRequest(job_id=job.id, artifact_id=target.artifact_id,
                                           offset=0, chunk_bytes=0),
                timeout=60,
            ):
                hasher.update(chunk.data)
                total += len(chunk.data)
            require(total == target.size_bytes,
                    f"下载字节数 {total} != 清单 {target.size_bytes}")
            require(hasher.hexdigest() == target.sha256,
                    f"sha256 不一致：{hasher.hexdigest()} != {target.sha256}")
            return f"下载 {target.relative_path} {total}B sha256 校验通过"

        report.run("DownloadArtifact：下载并校验 sha256", check_download)

        def check_download_resume():
            job = state["job"]
            # 同上：匹配 outputs.image 对应的顶层 render.png
            image_rel = f"{job.id}/render.png"
            target = next((a for a in job.artifacts if a.relative_path == image_rel), None)
            require(target is not None, f"manifest 中无顶层 {image_rel}")
            offset = max(1, target.size_bytes // 2)
            collected = b""
            for chunk in stub.DownloadArtifact(
                pb.DownloadArtifactRequest(job_id=job.id, artifact_id=target.artifact_id,
                                           offset=offset, chunk_bytes=0),
                timeout=60,
            ):
                collected += chunk.data
            # 用同一个 artifact 的 size_bytes 算 offset，用同一个文件比对
            expected = Path(job.outputs.image).read_bytes()[offset:]
            require(len(expected) == target.size_bytes - offset,
                    f"磁盘文件大小与清单不一致：磁盘切片={len(expected)} 清单={target.size_bytes - offset}")
            require(collected == expected, "续传内容与源文件切片不一致")
            return f"offset={offset} 续传 {len(collected)}B 一致"

        report.run("DownloadArtifact：offset 续传内容一致", check_download_resume)

        def check_ack():
            resp = stub.AckJob(pb.AckJobRequest(job_id=state["job"].id), timeout=30)
            # 新语义：Ack 只标记 acked=True，不立即释放产物
            require(resp.released is False, f"Ack 后 released 应为 false（新语义：不立即删），实际={resp.released}")
            require(resp.freed_bytes == 0, f"freed_bytes 应为 0（等保留期清理），实际={resp.freed_bytes}")
            # Ack 后产物仍在，下载仍可用
            target = next(a for a in state["job"].artifacts if a.name == "render.png")
            chunks = list(stub.DownloadArtifact(
                pb.DownloadArtifactRequest(job_id=state["job"].id,
                                           artifact_id=target.artifact_id), timeout=10))
            total = sum(len(c.data) for c in chunks)
            require(total > 0, f"Ack 后下载应仍可用，实际读到 0 字节")
            return f"released={resp.released} freed={resp.freed_bytes}B，Ack 后产物仍可下载 {total}B"

        report.run("AckJob：落盘确认标记，保留期内产物仍可下载", check_ack)

        def check_static_body():
            """BodySpec.mass 省略或为 0 → 静态/运动学刚体：不下落、velocity 恒为 0。"""
            def submit(bodies):
                req = pb.SubmitJobRequest(
                    request_id=uuid.uuid4().hex,
                    simulation=pb.SimulateRequest(
                        bodies=bodies, steps=30, dt=1 / 120, gravity=9.81, ground=True))
                jid = stub.SubmitJob(req, timeout=30).id
                deadline = time.monotonic() + args.timeout
                while time.monotonic() < deadline:
                    job = stub.GetJob(pb.GetJobRequest(job_id=jid), timeout=30)
                    if job.state not in {"queued", "running"}:
                        return job
                    time.sleep(args.interval)
                raise AssertionError(f"静态体任务等待超时: {jid}")

            def cube(body_id, mass=None):
                body = pb.BodySpec(id=body_id, shape="cube",
                                   position=pb.Vec3(x=0, y=0, z=1.0),
                                   size=pb.Vec3(x=.2, y=.2, z=.2))
                if mass is not None:
                    body.mass = mass
                return body

            results = {}
            for label, mass in (("省略", None), ("零", 0.0)):
                job = submit([cube(f"static-{label}", mass)])
                require(job.state == "succeeded", f"mass {label} 任务失败: {job.state} {job.error}")
                body = job.bodies[0]
                require(abs(body.pose_xyzw[2] - 1.0) < 1e-3,
                        f"mass {label} 应保持 z=1.0，实际 {body.pose_xyzw[2]:.4f}")
                require(all(abs(v) < 1e-6 for v in body.velocity),
                        f"mass {label} velocity 应全 0，实际 {list(body.velocity)}")
                results[label] = body.pose_xyzw[2]

            # 对照：显式正质量应下落，确认静态语义不是全局失效
            dynamic = submit([cube("dynamic", 1.0)])
            require(dynamic.state == "succeeded", f"动态任务失败: {dynamic.state}")
            require(dynamic.bodies[0].pose_xyzw[2] < 0.95,
                    f"mass=1.0 应下落，实际 z={dynamic.bodies[0].pose_xyzw[2]:.4f}")
            return (f"省略/零 mass 静止于 z={results['省略']:.3f}，mass=1.0 下落至 "
                    f"z={dynamic.bodies[0].pose_xyzw[2]:.3f}")

        report.run("BodySpec.mass：省略或 0 → 静态体（不下落、velocity=0）", check_static_body)

    return report.summary()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except grpc.RpcError as exc:
        print(f"\n无法连接服务：{exc.code().name}: {exc.details()}", file=sys.stderr)
        print("请先启动：bash start.sh", file=sys.stderr)
        raise SystemExit(2)
