"""gRPC 客户端命令行。

用法：
    python3 -m client.cli health
    python3 -m client.cli submit --scene cube
    python3 -m client.cli status --job-id <32位hex>
    python3 -m client.cli run --scene cube
    python3 -m client.cli list --job-id <32位hex>
    python3 -m client.cli download --job-id <32位hex> <artifact_id|name|relative_path> --out-dir ./out
    python3 -m client.cli ack --job-id <32位hex>
    python3 -m client.cli cancel --job-id <32位hex>
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
import uuid
from pathlib import Path

import grpc
from google.protobuf import json_format

from server import rpc

from . import scenes

pb = rpc.simulation_pb2
pb_grpc = rpc.simulation_pb2_grpc

MAX_MESSAGE = 32 * 1024 * 1024


def _channel(target: str) -> grpc.Channel:
    return grpc.insecure_channel(
        target,
        options=(
            ("grpc.max_send_message_length", MAX_MESSAGE),
            ("grpc.max_receive_message_length", MAX_MESSAGE),
        ),
    )


def _print(message) -> None:
    print(json_format.MessageToJson(message, preserving_proto_field_name=True, indent=2))


def _submit_req(scene: str, args) -> pb.SubmitJobRequest:
    simulation = scenes.apply_capture(
        scenes.load(scene), args.capture_every, args.capture_frames)
    request_id = (args.request_id or uuid.uuid4().hex).strip()
    return pb.SubmitJobRequest(request_id=request_id, simulation=simulation)


def _wait(stub, job_id: str, interval: float, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    last_state = None
    while True:
        job = stub.GetJob(pb.GetJobRequest(job_id=job_id), timeout=60)
        if job.state != last_state:
            progress = f" ({job.phase} {job.frames_done}/{job.frames_total})" if job.state == "running" else ""
            print(f"  state = {job.state}{progress}", file=sys.stderr)
            last_state = job.state
        if job.state not in {"queued", "running", ""}:
            _print(job)
            if job.state == "succeeded":
                out_dir = job.outputs.scene.rsplit("/", 1)[0] if job.outputs.scene else "(未知)"
                print(f"\n任务成功，产物目录（服务端）：{out_dir}", file=sys.stderr)
                print(f"共 {len(job.artifacts)} 个产物，可用 download 拉取", file=sys.stderr)
                return 0
            print(f"\n任务结束，状态={job.state} error={job.error}", file=sys.stderr)
            return 1
        if time.monotonic() > deadline:
            print(f"轮询超时（{timeout}s），任务仍在 {job.state}", file=sys.stderr)
            return 1
        time.sleep(interval)


def cmd_health(stub, args) -> int:
    _print(stub.Health(pb.HealthRequest(), timeout=args.timeout))
    return 0


def cmd_submit(stub, args) -> int:
    handle = stub.SubmitJob(_submit_req(args.scene, args), timeout=args.timeout)
    _print(handle)
    print(f"\n提交成功，查询：python3 -m client.cli status --job-id {handle.id}", file=sys.stderr)
    return 0


def cmd_status(stub, args) -> int:
    _print(stub.GetJob(pb.GetJobRequest(job_id=args.job_id), timeout=args.timeout))
    return 0


def cmd_run(stub, args) -> int:
    handle = stub.SubmitJob(_submit_req(args.scene, args), timeout=args.timeout)
    print(f"job_id = {handle.id}，开始轮询...", file=sys.stderr)
    return _wait(stub, handle.id, args.interval, args.wait_timeout)


def cmd_list_artifacts(stub, args) -> int:
    job = stub.GetJob(pb.GetJobRequest(job_id=args.job_id), timeout=args.timeout)
    if not job.artifacts:
        print("（无产物清单，任务可能尚未 succeeded）", file=sys.stderr)
    for a in job.artifacts:
        print(f"{a.artifact_id}  {a.size_bytes:>12}  {a.media_type:<22} {a.relative_path}")
    return 0


def cmd_download(stub, args) -> int:
    job = stub.GetJob(pb.GetJobRequest(job_id=args.job_id), timeout=args.timeout)
    target = args.artifact
    artifact = None
    for a in job.artifacts:
        if target in (a.artifact_id, a.relative_path, a.name):
            artifact = a
            break
    if artifact is None:
        print(f"未找到产物: {target}", file=sys.stderr)
        return 1
    out_dir = args.out_dir or (Path("./downloads") / args.job_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / artifact.name
    hasher = hashlib.sha256()
    written = 0
    with out_path.open("wb") as fh:
        for chunk in stub.DownloadArtifact(
            pb.DownloadArtifactRequest(job_id=args.job_id, artifact_id=artifact.artifact_id,
                                       offset=0, chunk_bytes=args.chunk_bytes),
            timeout=args.timeout,
        ):
            fh.write(chunk.data)
            hasher.update(chunk.data)
            written += len(chunk.data)
    digest = hasher.hexdigest()
    print(f"已下载 {out_path}  {written} 字节  sha256={digest}", file=sys.stderr)
    if digest != artifact.sha256:
        print(f"WARN: sha256 不一致！期望 {artifact.sha256}", file=sys.stderr)
        return 2
    return 0


def cmd_ack(stub, args) -> int:
    resp = stub.AckJob(pb.AckJobRequest(job_id=args.job_id), timeout=args.timeout)
    _print(resp)
    return 0


def cmd_cancel(stub, args) -> int:
    job = stub.CancelJob(pb.GetJobRequest(job_id=args.job_id), timeout=args.timeout)
    _print(job)
    return 0


def _frames(value: str) -> list[int]:
    return [int(item) for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isaac Sim gRPC 客户端")
    parser.add_argument("--target", default="127.0.0.1:18500", help="gRPC 服务地址")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次 RPC 超时秒数")
    parser.add_argument("--request-id", default=None, help="幂等键；缺省自动生成")
    parser.add_argument("--chunk-bytes", type=int, default=0, help="下载块大小，0=服务端默认")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="健康检查").set_defaults(func=cmd_health)

    submit = sub.add_parser("submit", help="提交仿真任务")
    submit.add_argument("--scene", required=True, help="内置场景（cube/stack/sphere_rain）或 JSON 文件")
    submit.add_argument("--capture-every", type=int, help="每 N 帧保存一次（含逐帧渲染）")
    submit.add_argument("--capture-frames", type=_frames, help="指定帧号，逗号分隔，如 10,50,120")
    submit.set_defaults(func=cmd_submit)

    status = sub.add_parser("status", help="查询任务")
    status.add_argument("--job-id", required=True)
    status.set_defaults(func=cmd_status)

    run = sub.add_parser("run", help="提交并等待任务完成")
    run.add_argument("--scene", required=True, help="内置场景（cube/stack/sphere_rain）或 JSON 文件")
    run.add_argument("--capture-every", type=int, help="每 N 帧保存一次（含逐帧渲染）")
    run.add_argument("--capture-frames", type=_frames, help="指定帧号，逗号分隔，如 10,50,120")
    run.add_argument("--interval", type=float, default=0.5, help="轮询间隔秒数")
    run.add_argument("--wait-timeout", type=float, default=600.0, help="总等待超时秒数")
    run.set_defaults(func=cmd_run)

    lst = sub.add_parser("list", help="列出任务产物清单")
    lst.add_argument("--job-id", required=True)
    lst.set_defaults(func=cmd_list_artifacts)

    dl = sub.add_parser("download", help="下载单个产物并校验 sha256")
    dl.add_argument("--job-id", required=True)
    dl.add_argument("artifact", help="artifact_id / name / relative_path")
    dl.add_argument("--out-dir", type=Path, default=None, help="输出目录，默认 ./downloads/<job_id>")
    dl.set_defaults(func=cmd_download)

    ack = sub.add_parser("ack", help="确认落盘，允许服务端回收")
    ack.add_argument("--job-id", required=True)
    ack.set_defaults(func=cmd_ack)

    cancel = sub.add_parser("cancel", help="取消排队或运行中的任务")
    cancel.add_argument("--job-id", required=True)
    cancel.set_defaults(func=cmd_cancel)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with _channel(args.target) as channel:
        stub = pb_grpc.SimulationServiceStub(channel)
        try:
            return args.func(stub, args)
        except grpc.RpcError as exc:
            print(f"RPC 失败: {exc.code().name}: {exc.details()}", file=sys.stderr)
            return 1
        except FileNotFoundError as exc:
            print(f"场景错误: {exc}", file=sys.stderr)
            return 2


if __name__ == "__main__":
    raise SystemExit(main())
