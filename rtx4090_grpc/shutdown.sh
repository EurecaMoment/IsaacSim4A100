#!/usr/bin/env bash
# 停止 gRPC 服务（优先优雅退出，超时后强制终止）。
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib/common.sh"
load_env "$ADAPTER_DIR/.env"

port="$(grpc_port)"
pidfile="$(grpc_pid_file)"
pid="$(read_pid)"

if ! pid_alive "$pid"; then
    # 兜底：PID 文件丢失时按端口查找
    pid="$(port_pid "$port")"
fi

if ! pid_alive "$pid"; then
    echo "未检测到运行中的 gRPC 服务（port=$port）"
    rm -f "$pidfile"
    exit 0
fi

echo "停止 gRPC 服务：pid=$pid port=$port"
if ! kill -TERM -"$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
fi

timeout="${GRPC_STOP_TIMEOUT:-30}"
waited=0
while pid_alive "$pid" && (( waited < timeout )); do
    sleep 1
    waited=$((waited + 1))
done

if pid_alive "$pid"; then
    echo "优雅退出超时（${timeout}s），强制终止" >&2
    kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    sleep 1
fi

rm -f "$pidfile"
echo "已停止（port=$port 已释放）"
