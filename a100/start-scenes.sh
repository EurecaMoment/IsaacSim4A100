#!/usr/bin/env bash
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/config.sh"
export ISAAC_GPU="${A100SIM_SCENE_GPU:-$ISAAC_GPU}"
source "$A100SIM_CODE_ROOT/resolve-gpu.sh"
export A100SIM_STATE_ROOT="$A100SIM_ROOT/state/scenes"
export A100SIM_KIT_ROOT="$A100SIM_STATE_ROOT/kit"
export A100SIM_LOCAL_STATE="$A100SIM_LOCAL_STATE/scenes"
umask 077
exec bash "$A100SIM_CODE_ROOT/run-python.sh" -m a100sim.scenes.service \
  --output "$A100SIM_ROOT/results/scene-service" --port "${A100SIM_SCENE_PORT:-18324}" "$@"
