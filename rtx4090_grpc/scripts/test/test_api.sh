#!/usr/bin/env bash
# 调用已启动的 gRPC 服务做接口测试（目标地址取自 .env）。
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/../lib/common.sh"
load_env "$ADAPTER_DIR/.env"

target="${GRPC_HOST:-127.0.0.1}:${GRPC_PORT:-18500}"
exec "$ADAPTER_DIR/.venv/bin/python" "$ADAPTER_DIR/tests/test_api.py" --target "$target" "$@"
