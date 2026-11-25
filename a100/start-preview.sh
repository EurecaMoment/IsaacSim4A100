#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/config.sh"
export ISAAC_GPU="${A100SIM_SCENE_GPU:-$ISAAC_GPU}"
export A100SIM_RENDER_VARIANT=llvm_ad_rgb
export DRJIT_LIBLLVM_PATH="${A100SIM_PREVIEW_LLVM:-$(dirname "$(dirname "$A100SIM_PYTHON")")/lib/libLLVM-20.so}"
[[ -r "$DRJIT_LIBLLVM_PATH" ]]
export A100SIM_STATE_ROOT="$A100SIM_ROOT/state/preview"
export A100SIM_KIT_ROOT="$A100SIM_STATE_ROOT/kit"
export A100SIM_LOCAL_STATE="$A100SIM_LOCAL_STATE/preview"
export TMPDIR="$A100SIM_ROOT/tmp/preview"
mkdir -p "$TMPDIR"
umask 077
exec bash "$A100SIM_CODE_ROOT/run-python.sh" -m a100sim.scenes.preview_service \
  --sessions "$A100SIM_ROOT/results/scene-service/sessions" \
  --port "${A100SIM_PREVIEW_PORT:-18325}" --capacity "${A100SIM_PREVIEW_CAPACITY:-32}"
