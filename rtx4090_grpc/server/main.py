"""gRPC 服务入口（自带 Isaac Sim 引擎，与 HTTP 版平级）。

用法：
    bash scripts/system/run_server.sh
    bash start.sh            # 后台启动（推荐）
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from concurrent import futures
from dataclasses import replace
from pathlib import Path

import grpc

from . import config, rpc
from .engine import Engine
from .servicer import SimulationServicer

MAX_MESSAGE = 32 * 1024 * 1024


def build_server(settings: config.Settings, state) -> grpc.Server:
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=settings.max_workers, thread_name_prefix="grpc"),
        options=(
            ("grpc.max_send_message_length", MAX_MESSAGE),
            ("grpc.max_receive_message_length", MAX_MESSAGE),
        ),
    )
    rpc.simulation_pb2_grpc.add_SimulationServiceServicer_to_server(SimulationServicer(state), server)
    if settings.reflection:
        try:
            from grpc_reflection.v1alpha import reflection

            names = (
                rpc.simulation_pb2.DESCRIPTOR.services_by_name["SimulationService"].full_name,
                reflection.SERVICE_NAME,
            )
            reflection.enable_server_reflection(names, server)
        except ImportError:
            print("WARN: 未安装 grpcio-reflection，跳过服务反射注册", file=sys.stderr)
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Isaac Sim RTX 4090 gRPC 服务")
    parser.add_argument("--host", help="监听地址，默认取 GRPC_HOST 或 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认取 GRPC_PORT 或 18500")
    parser.add_argument("--gpu", type=int, help="物理 GPU 序号，默认取 ISAAC_GPU 或 0")
    parser.add_argument("--state-root", type=Path, help="产物根目录，默认取 RTX4090_GRPC_STATE_ROOT")
    parser.add_argument("--max-workers", type=int, help="gRPC 工作线程数")
    parser.add_argument("--queue-size", type=int, help="作业队列容量")
    parser.add_argument("--env-file", type=Path, default=config.DEFAULT_ENV_FILE, help="环境文件路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = config.Settings.load(args.env_file)
    overrides = {
        "host": args.host,
        "port": args.port,
        "isaac_gpu": args.gpu,
        "state_root": args.state_root,
        "max_workers": args.max_workers,
        "capacity": args.queue_size,
    }
    settings = replace(settings, **{key: value for key, value in overrides.items() if value is not None})

    stop = threading.Event()

    def shutdown(signum, _frame) -> None:
        print(f"收到信号 {signum}，正在停止服务...", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"加载 Isaac Sim 引擎: {settings.isaac_sim_dir} (gpu={settings.isaac_gpu})", flush=True)
    engine = Engine(settings)
    server = build_server(settings, engine.queue)
    address = f"{settings.host}:{settings.port}"
    if server.add_insecure_port(address) == 0:
        print(f"ERROR: 无法绑定 {address}", file=sys.stderr)
        engine.close()
        return 1

    server.start()
    print(
        f"GRPC_SERVICE_READY address={address} gpu={settings.isaac_gpu} "
        f"output={settings.output} reflection={'on' if settings.reflection else 'off'}",
        flush=True,
    )
    try:
        engine.serve_forever(stop)
    finally:
        server.stop(grace=5).wait()
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
