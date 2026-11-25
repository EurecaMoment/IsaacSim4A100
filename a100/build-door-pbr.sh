#!/usr/bin/env bash
# Build against the headers shipped with the pinned Mitsuba/Dr.Jit runtime.
set -euo pipefail
source "$(dirname -- "${BASH_SOURCE[0]}")/config.sh"
packages="$A100SIM_ROOT/render-packages"
source_file="$A100SIM_CODE_ROOT/scripts/mi_door_pbr.cpp"
target="$packages/mitsuba/plugins/bf_door_pbr.so"
if [[ "$target" -nt "$source_file" && "$target" -nt "$packages/mitsuba/libmitsuba.so" ]]; then
    exit 0
fi
headers="${A100SIM_NANOBIND_ROOT:-$A100SIM_ROOT/tools/door-pbr-headers/nanobind}"
if [[ ! -f "$headers/include/nanobind/intrusive/ref.h" ]]; then
    uv pip install --python "$A100SIM_PYTHON" --target "$(dirname "$headers")" nanobind==2.10.2
fi
binary="$(mktemp "$packages/mitsuba/plugins/.bf-door-pbr.XXXXXX")"
trap 'rm -f -- "$binary"' EXIT
# The x86_64 Mitsuba wheel uses the four-lane SSE ABI for three-channel colors.
# Without SSE4.2 the plugin can link but return a corrupt blue component.
"${CXX:-c++}" -std=c++17 -msse4.2 -O3 -DNDEBUG -fPIC -shared \
    -I"$packages/mitsuba/include" -I"$packages/drjit/include" \
    -I"$headers/include" -I"$headers/ext/robin_map/include" \
    "$source_file" -L"$packages/mitsuba" -lmitsuba -o "$binary"
chmod 755 "$binary"
mv -f -- "$binary" "$target"
