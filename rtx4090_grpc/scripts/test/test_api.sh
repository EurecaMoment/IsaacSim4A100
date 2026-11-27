#!/usr/bin/env bash
# 三层测试入口：
#   L1 JobManager 纯逻辑单元测试（无 Isaac、无 gRPC，秒级）
#   L2 Servicer RPC 协议层单元测试（内存 gRPC stub，秒级）
#   L3 端到端集成测试（需真实 Isaac gRPC 服务在监听）
#
# 用法：
#   bash scripts/test/test_api.sh                 # 跑全部三层
#   bash scripts/test/test_api.sh --only L1       # 只跑 L1
#   bash scripts/test/test_api.sh --only L2       # 只跑 L2
#   bash scripts/test/test_api.sh --only L3       # 只跑 L3
#   bash scripts/test/test_api.sh --skip L3       # 跳过 L3（服务未起时）
#   bash scripts/test/test_api.sh --scene cube --capture-every 20   # 透传给 L3
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADAPTER_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$ADAPTER_DIR/scripts/lib/common.sh" 2>/dev/null || true
load_env "$ADAPTER_DIR/.env" 2>/dev/null || true

PY="$ADAPTER_DIR/.venv/bin/python"
TESTS="$ADAPTER_DIR/tests"

# ---------- 参数解析 ----------
ONLY=""
SKIP=""
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)   ONLY="$2"; shift 2 ;;
        --skip)   SKIP="$2"; shift 2 ;;
        -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)        EXTRA_ARGS+=("$1"); shift ;;
    esac
done

should_run() {
    local level="$1"
    [[ -n "$ONLY" ]] && [[ "$ONLY" != "$level" ]] && return 1
    [[ -n "$SKIP" ]] && [[ "$SKIP" == "$level" ]] && return 1
    return 0
}

# ---------- 彩色输出 ----------
PASS=0
FAIL=0
_total=0

log()   { printf '%s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; PASS=$((PASS+1)); _total=$((_total+1)); }
ng()    { printf '  \033[31m✗\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); _total=$((_total+1)); }
header(){ printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# ---------- L1 ----------
if should_run L1; then
    header "L1 JobManager 单元测试（无 Isaac、无 gRPC）"
    if "$PY" "$TESTS/test_job_manager.py"; then
        ok "test_job_manager.py"
    else
        ng "test_job_manager.py"
    fi
fi

# ---------- L2 ----------
if should_run L2; then
    header "L2 Servicer RPC 协议层单元测试（内存 stub）"
    if "$PY" "$TESTS/test_servicer_unit.py"; then
        ok "test_servicer_unit.py"
    else
        ng "test_servicer_unit.py"
    fi
fi

# ---------- L3 ----------
if should_run L3; then
    header "L3 端到端集成测试（需 Isaac gRPC 服务在监听）"

    host="${GRPC_HOST:-127.0.0.1}"
    port="${GRPC_PORT:-18500}"
    target="$host:$port"

    # 检测端口
    if ! (command -v ss >/dev/null 2>&1 && ss -tlnp 2>/dev/null | grep -q ":${port} ") \
        && ! (command -v netstat >/dev/null 2>&1 && netstat -tln 2>/dev/null | grep -q ":${port} ") \
        && ! (echo > /dev/tcp/"$host"/"$port" 2>/dev/null); then
        ng "L3 跳过：服务未在 $target 监听。先执行 bash start.sh --wait"
    else
        log "目标: $target"

        # 默认冒烟：submit+幂等+get+download+ack，capture 场景验证进度与逐帧
        if ! "$PY" "$TESTS/smoke_test.py" --target "$target" "${EXTRA_ARGS[@]}"; then
            ng "smoke_test.py"
        else
            ok "smoke_test.py（Health / Submit幂等 / Get清单+进度 / Download+sha256 / Ack回收）"
        fi

        # 完整场景覆盖（cube + capture=every 20，逐帧采集+渲染+产物清单）
        scene="${EXTRA_ARGS[*]:-}"
        [[ -z "$scene" ]] && scene="--scene cube --capture-every 20"
        if ! "$PY" "$TESTS/test_api.py" --target "$target" $scene; then
            ng "test_api.py $scene"
        else
            ok "test_api.py（全场景）"
        fi
    fi
fi

# ---------- 汇总 ----------
header "汇总"
printf '通过 \033[32m%d\033[0m / \033[1m%d\033[0m' "$PASS" "$_total"
if [[ "$FAIL" -gt 0 ]]; then
    printf '  \033[31m失败 %d\033[0m' "$FAIL"
fi
printf '\n'

[[ "$FAIL" -eq 0 ]]
