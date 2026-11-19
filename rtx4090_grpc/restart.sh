#!/usr/bin/env bash
# 重启 gRPC 服务。
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$script_dir/shutdown.sh"
exec bash "$script_dir/start.sh"
