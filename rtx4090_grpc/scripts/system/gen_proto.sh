#!/usr/bin/env bash
# 由 proto 生成 Python 代码到 generated/。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python="$root/.venv/bin/python"
[[ -x "$python" ]] || python="$(command -v python3)"

if ! "$python" -c "import grpc_tools" >/dev/null 2>&1; then
    echo "缺少 grpcio-tools，请先执行 bash scripts/system/setup_venv.sh" >&2
    exit 1
fi

mkdir -p "$root/generated"
"$python" -m grpc_tools.protoc \
    -I "$root/proto" \
    --python_out="$root/generated" \
    --grpc_python_out="$root/generated" \
    --pyi_out="$root/generated" \
    "$root/proto/isaacsim_rpc/v1/native.proto" \
    "$root/proto/isaacsim_rpc/v1/simulation.proto"

echo "已生成："
find "$root/generated" -name '*_pb2*.py' -o -name '*_pb2*.pyi' | sort
