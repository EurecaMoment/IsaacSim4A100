#!/usr/bin/env bash
# 创建隔离 venv 并安装依赖，生成 protobuf 代码，并为 Isaac Python 安装 gRPC 运行库。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root"

if [[ ! -x .venv/bin/python ]]; then
    echo "[1/4] 创建 venv: $root/.venv"
    python3 -m venv .venv
else
    echo "[1/4] 复用已有 venv"
fi

echo "[2/4] 安装开发依赖（客户端 / 代码生成）"
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

echo "[3/4] 生成 protobuf 代码"
bash "$root/scripts/system/gen_proto.sh"

echo "[4/4] 安装 Isaac Python 3.12 运行库"
bash "$root/scripts/system/install_runtime_deps.sh"

echo "完成。启动：bash start.sh"
