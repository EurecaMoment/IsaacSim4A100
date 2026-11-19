#!/usr/bin/env bash
# 手动启动 gRPC 服务（后台常驻 + PID/日志管理）。
#   bash start.sh                 # 立即返回，服务在后台加载
#   bash start.sh --wait          # 阻塞等待就绪（适合脚本编排）
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/scripts/lib/common.sh"

wait_ready=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --wait) wait_ready=1; shift ;;
        -h|--help) sed -n '1,4p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数：$1（可用 --wait）" >&2; exit 2 ;;
    esac
done
load_env "$ADAPTER_DIR/.env"
load_env "$REPO_DIR/rtx4090/.env"

port="$(grpc_port)"
state="$(grpc_state_dir)"
pidfile="$(grpc_pid_file)"
logfile="$(grpc_log_file)"
mkdir -p "$state"

running_pid="$(read_pid)"
if pid_alive "$running_pid"; then
    echo "已在运行：pid=$running_pid port=$port"
    echo "如需重启请执行：bash restart.sh"
    exit 0
fi

occupied="$(port_pid "$port")"
if [[ -n "$occupied" ]]; then
    echo "端口 $port 已被进程 $occupied 占用，请先停止该进程或修改 .env 中的 GRPC_PORT" >&2
    exit 1
fi

if [[ ! -d "$ADAPTER_DIR/runtime_python" ]]; then
    echo "缺少 runtime_python，请先执行：bash scripts/system/install_runtime_deps.sh" >&2
    exit 1
fi
if [[ ! -d "$ADAPTER_DIR/generated/isaacsim_rpc" ]]; then
    echo "缺少 generated/，请先执行：bash scripts/system/gen_proto.sh" >&2
    exit 1
fi

: > "$logfile"
echo "启动 gRPC 服务：port=$port gpu=${ISAAC_GPU:-?} sim=${ISAAC_SIM_DIR:-?}"
echo "日志：$logfile（独立会话后台运行，Ctrl+C 不会停掉它）"

if command -v setsid >/dev/null 2>&1; then
    setsid bash "$ADAPTER_DIR/scripts/system/run_server.sh" --port "$port" >>"$logfile" 2>&1 < /dev/null &
else
    nohup bash "$ADAPTER_DIR/scripts/system/run_server.sh" --port "$port" >>"$logfile" 2>&1 < /dev/null &
fi
launcher=$!
echo "$launcher" > "$pidfile"
sleep 1

if ! pid_alive "$launcher"; then
    echo "启动失败：进程已退出，日志尾部：" >&2
    tail -30 "$logfile" >&2
    rm -f "$pidfile"
    exit 1
fi

if (( wait_ready == 0 )); then
    echo "已在后台启动：pid=$launcher port=$port"
    echo "就绪检测：bash scripts/test/test_api.sh    （或 tail -f $logfile）"
    echo "停止：bash shutdown.sh"
    exit 0
fi

echo "正在加载 Isaac Sim，通常 60~120 秒；每 15 秒打印一次进度。"
timeout="${GRPC_START_TIMEOUT:-300}"
waited=0
while (( waited < timeout )); do
    if grep -q "GRPC_SERVICE_READY" "$logfile" 2>/dev/null; then
        echo ""
        echo "启动成功：pid=$launcher port=$port"
        echo "停止：bash shutdown.sh"
        exit 0
    fi
    if ! pid_alive "$launcher"; then
        echo ""
        echo "启动失败：进程已退出，日志尾部：" >&2
        tail -30 "$logfile" >&2
        rm -f "$pidfile"
        exit 1
    fi
    # 心跳：每 15 秒给出已等待时间与日志最后一行
    if (( waited % 15 == 0 )); then
        last="$(grep -v '^[[:space:]]*$' "$logfile" 2>/dev/null | tail -1 | cut -c1-110)"
        printf '[%3ss] 等待中... %s\n' "$waited" "${last:-无新日志}"
    fi
    sleep 3
    waited=$((waited + 3))
done

echo "等待超时（${timeout}s），服务可能仍在加载；请查看日志：$logfile" >&2
tail -20 "$logfile" >&2
exit 1
