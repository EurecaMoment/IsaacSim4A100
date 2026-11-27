#!/usr/bin/env bash
# 公共函数：路径解析、.env 加载、PID 与端口检查。由其他脚本 source 使用。

_common_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # scripts/lib
ADAPTER_DIR="$(dirname "$(dirname "$_common_dir")")"          # 项目根目录
REPO_DIR="$(dirname "$ADAPTER_DIR")"                          # IsaacSim4A100

# 只补充尚未设置的环境变量，避免 .env 覆盖命令行/父 shell 的显式赋值
load_env() {
    local file="$1" line key value
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%%#*}"
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" != *=* ]] && continue
        key="${line%%=*}"
        value="${line#*=}"
        key="${key//[[:space:]]/}"
        value="${value%\"}"
        value="${value#\"}"
        value="${value%\'}"
        value="${value#\'}"
        [[ -n "${!key:-}" ]] || export "$key=$value"
    done < "$file"
}

grpc_host() { echo "${GRPC_HOST:-127.0.0.1}"; }
grpc_port() { echo "${GRPC_PORT:-18500}"; }
grpc_state_dir() { echo "${RTX4090_GRPC_STATE_ROOT:-$ADAPTER_DIR/state}"; }
grpc_pid_file() { echo "$(grpc_state_dir)/grpc.pid"; }
grpc_log_file() { echo "$(grpc_state_dir)/grpc.log"; }

pid_alive() { [[ -n "${1:-}" ]] && kill -0 "$1" 2>/dev/null; }

read_pid() {
    local file
    file="$(grpc_pid_file)"
    [[ -f "$file" ]] && tr -d '[:space:]' < "$file" || true
}

# 通过 ss 反查监听指定端口的进程号
port_pid() {
    local port="$1"
    ss -ltnp 2>/dev/null \
        | grep -E "[:.]${port}[[:space:]]" \
        | sed -nE 's/.*pid=([0-9]+).*/\1/p' \
        | head -1 || true
}
