#!/usr/bin/env bash
set -euo pipefail

if (( $# < 5 )); then
    echo 'usage: build-node.sh <src-engine> <src-trellis|-> <build-dir> <out-bin> <targets...>' >&2
    exit 2
fi
engine=$1 trellis=$2 build=$3 out=$4
shift 4
mkdir -p -- "$build" "$out"
export PATH="/opt/rocm/core-10.1/bin:$PATH"
export HIPCXX=/opt/rocm/core-10.1/bin/amdclang++
export ROCM_PATH=/opt/rocm/core-10.1
export HIP_PATH=/opt/rocm/core-10.1
configure=()
while IFS= read -r flag; do
    [[ -z "$flag" ]] || configure+=("$flag")
done <<< "${HYPERLOOM_CONFIGURE:-}"
# Per-family trellis recipe (hyperloom/engines.py): ds41 links the archive through STRIX_DS41_TRELLIS_*
# (adapter HYPERLOOM_TRELLIS_API, v12 unless the preset says otherwise); minimax points STRIX_MINIMAX_TRELLIS_DIR at
# a tree with include/strix_trellis.h and build-qualification-v03/libstrix_trellis.a, the layout its CMake expects.
mode=${HYPERLOOM_TRELLIS_MODE:-ds41}
api=${HYPERLOOM_TRELLIS_API:-v12}
trellis_configure=()
while IFS= read -r flag; do
    [[ -z "$flag" ]] || trellis_configure+=("$flag")
done <<< "${HYPERLOOM_TRELLIS_CONFIGURE:-}"
if [[ "$trellis" != - ]]; then
    case "$mode" in
        ds41) trellis_build="$build/trellis" ;;
        minimax)
            mkdir -p -- "$build/mm-trellis"
            ln -sfn -- "$trellis/include" "$build/mm-trellis/include"
            trellis_build="$build/mm-trellis/build-qualification-v03" ;;
        *) echo "unknown trellis mode: $mode" >&2; exit 2 ;;
    esac
    cmake -S "$trellis" -B "$trellis_build" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DSTRIX_TRELLIS_ENABLE_HIP=ON \
        -DCMAKE_HIP_COMPILER="$HIPCXX" -DCMAKE_HIP_ARCHITECTURES=gfx1151 "${trellis_configure[@]}"
    ninja -C "$trellis_build" -j6 strix_trellis
    if [[ "$mode" == minimax ]]; then
        configure+=("-DSTRIX_MINIMAX_TRELLIS_DIR=$build/mm-trellis")
    else
        configure+=(-DSTRIX_DS41_TRELLIS=ON "-DSTRIX_DS41_TRELLIS_API=$api"
            "-DSTRIX_DS41_TRELLIS_LIBRARY=$build/trellis/libstrix_trellis.a"
            "-DSTRIX_DS41_TRELLIS_INCLUDE_DIR=$trellis/include")
    fi
fi
cmake -S "$engine" -B "$build/engine" -G Ninja "${configure[@]}"
regex=${HYPERLOOM_CTEST_REGEX:?a named host-only ctest subset is required}
ctest --test-dir "$build/engine" --show-only=json-v1 -R "$regex" > "$build/tests.json"
# CTest provides the executable paths, so only the selected host test targets are built.
python3 - "$build/tests.json" > "$build/test-targets" <<'PY'
import json, pathlib, re, sys
tests = json.loads(pathlib.Path(sys.argv[1]).read_text())["tests"]
if not tests:
    raise SystemExit("ctest regex selected no host tests")
targets = set()
for test in tests:
    # CTest omits command when the executable has not been built yet.
    target = pathlib.Path(test.get("command", [test["name"]])[0]).name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", target):
        raise SystemExit("invalid host test target")
    if re.search(r"hip|cuda|gpu", target, re.I):
        raise SystemExit("GPU tests are forbidden in node build jobs")
    targets.add(target)
print("\n".join(sorted(targets)))
PY
mapfile -t test_targets < "$build/test-targets"
ninja -C "$build/engine" -j6 "$@" "${test_targets[@]}"
ctest --test-dir "$build/engine" --output-on-failure -R "$regex"
for target in "$@"; do
    cp -- "$build/engine/$target" "$out/$target"
done
