#!/usr/bin/env bash
# 启动 gRPC 服务：自行加载 Isaac Sim 引擎，与 HTTP 版（rtx4090/run.sh）平级。
set -euo pipefail

adapter_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
repo_dir="$(dirname "$adapter_dir")"

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

# 先读本服务 gRPC 配置（ISAAC_GPU 等服务特有字段优先），再补引擎公共配置
load_env "$adapter_dir/.env"
load_env "$repo_dir/rtx4090/.env"

export ISAAC_SIM_DIR="${ISAAC_SIM_DIR:-$repo_dir/_build/linux-x86_64/release}"
export ISAAC_GPU="${ISAAC_GPU:-0}"
# Kit 的缓存与 HTTP 版分开，避免两个实例共用 portable-root
export RTX4090_STATE_ROOT="${RTX4090_STATE_ROOT:-$adapter_dir/state/rtx}"
export RTX4090_GRPC_STATE_ROOT="${RTX4090_GRPC_STATE_ROOT:-$adapter_dir/state}"
export A100SIM_ASSET_ROOT="${RTX4090_ASSET_ROOT:-$repo_dir/rtx4090/assets}"

if [[ ! -x "$ISAAC_SIM_DIR/python.sh" ]]; then
    echo "Set ISAAC_SIM_DIR to an official Isaac Sim standalone installation." >&2
    exit 1
fi
if [[ ! -d "$adapter_dir/runtime_python" ]]; then
    echo "缺少 runtime_python（Isaac Python 3.12 的 gRPC 运行库），请先执行：bash scripts/system/install_runtime_deps.sh" >&2
    exit 1
fi
if [[ -n "${CUDA_VISIBLE_DEVICES:-}" ]]; then
    echo "Use ISAAC_GPU as a physical GPU index and launch without CUDA_VISIBLE_DEVICES." >&2
    exit 1
fi
if [[ ! "$ISAAC_GPU" =~ ^[0-9]+$ ]]; then
    echo "ISAAC_GPU must be a physical nvidia-smi index." >&2
    exit 1
fi

selection="$(python3 "$repo_dir/deployment/detect_gpu.py" --gpu "$ISAAC_GPU" --backend rtx --format shell)"
eval "$selection"
export RTX4090_GPU_UUID="$ISAACSIM_GPU_UUID"
# 物理 GPU 0 同时也是 CUDA 序号 0 时才需要限制可见设备
if [[ "$ISAAC_GPU" = 0 ]]; then
    export CUDA_VISIBLE_DEVICES="$RTX4090_GPU_UUID"
fi

export PYTHONPATH="$repo_dir:$repo_dir/a100:$adapter_dir/runtime_python${PYTHONPATH:+:$PYTHONPATH}"
export OMNI_KIT_ALLOW_ROOT=1
export OMNI_KIT_ACCEPT_EULA=YES
export PYTHONUNBUFFERED=1

# Isaac Sim 要求干净的动态库搜索路径；被 conda/容器工具污染时会报 GLIBCXX 版本错误
if [[ "${ISAACSIM_KEEP_LD_LIBRARY_PATH:-0}" != "1" ]]; then
    unset LD_LIBRARY_PATH
fi

mkdir -p "$RTX4090_GRPC_STATE_ROOT" "$RTX4090_STATE_ROOT" "$A100SIM_ASSET_ROOT"
cd "$adapter_dir"
exec "$ISAAC_SIM_DIR/python.sh" -m server.main "$@"
