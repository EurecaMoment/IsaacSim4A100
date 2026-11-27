#!/usr/bin/env bash
# 为 Isaac Sim 自带的 Python 3.12 安装 gRPC 运行库到 runtime_python/，
# 不修改 Isaac Sim 安装本身。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python="$root/.venv/bin/python"
[[ -x "$python" ]] || python="$(command -v python3)"

mkdir -p "$root/runtime_python"
"$python" -m pip install --target "$root/runtime_python" --no-deps --only-binary=:all: \
    --python-version 3.12 --implementation cp --abi cp312 --platform manylinux2014_x86_64 \
    -r "$root/requirements-runtime.txt"

echo "运行库已安装到 $root/runtime_python"
