"""gRPC 客户端命令行。

用法：
    python3 -m client.cli health
    python3 -m client.cli submit --scene cube
    python3 -m client.cli status --job-id <32位hex>
    python3 -m client.cli run --scene cube
"""

from __future__ import annotations

import argparse
import sys
import time

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


def _wait(stub, job_id: str, interval: float, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    last_state = None
    while True:
        job = stub.GetJob(pb.GetJobRequest(job_id=job_id), timeout=60)
        if job.state != last_state:
            print(f"  state = {job.state}", file=sys.stderr)
            last_state = job.state
        if job.state not in {"queued", "running", ""}:
            _print(job)
            if job.state == "succeeded":
                print(f"\n任务成功，产物目录（服务端）：{job.artifacts.scene.rsplit('/', 1)[0]}", file=sys.stderr)
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
    request = scenes.apply_capture(
        scenes.load(args.scene), args.capture_every, args.capture_frames)
    handle = stub.SubmitJob(request, timeout=args.timeout)
    _print(handle)
    print(f"\n提交成功，查询：python3 -m client.cli status --job-id {handle.id}", file=sys.stderr)
    return 0


def cmd_status(stub, args) -> int:
    _print(stub.GetJob(pb.GetJobRequest(job_id=args.job_id), timeout=args.timeout))
    return 0


def cmd_run(stub, args) -> int:
    request = scenes.apply_capture(
        scenes.load(args.scene), args.capture_every, args.capture_frames)
    handle = stub.SubmitJob(request, timeout=args.timeout)
    print(f"job_id = {handle.id}，开始轮询...", file=sys.stderr)
    return _wait(stub, handle.id, args.interval, args.wait_timeout)


def _frames(value: str) -> list[int]:
    return [int(item) for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Isaac Sim gRPC 客户端")
    parser.add_argument("--target", default="127.0.0.1:18500", help="gRPC 服务地址")
    parser.add_argument("--timeout", type=float, default=60.0, help="单次 RPC 超时秒数")
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="健康检查")
    health.set_defaults(func=cmd_health)

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
