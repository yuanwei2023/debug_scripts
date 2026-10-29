#!/usr/bin/env bash
# Build (if needed) and run the controllable GPU test workload so the
# collectors have a live PID with busy KFD queues. All options after the
# script's own ones are passed to the binary; see --help.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SRC=$SCRIPT_DIR/test_kernel/gpu_queue_test.hip
BUILD_DIR=${BUILD_DIR:-$SCRIPT_DIR/test_kernel/build}
BIN=$BUILD_DIR/gpu_queue_test
HIPCC=${HIPCC:-hipcc}

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat << USAGE
Usage: ${0##*/} [--rebuild] [workload options]

Examples:
  ${0##*/}                                  # GPU 0, spinning waves + 8 pending noops
  ${0##*/} --gpus all --mode spin,wait,sdma # every GPU, three queues each
  ${0##*/} --gpus 0,1 --mode wait --pending 32
  ${0##*/} --mode spin --duration 120 --pid-file /tmp/gpu_test.pid

Env: HIPCC (default hipcc), OFFLOAD_ARCH (default: detected via rocminfo),
     BUILD_DIR (default test_kernel/build)

Workload options:
USAGE
}

rebuild=0
if [[ ${1:-} == --rebuild ]]; then
    rebuild=1
    shift
fi

if (( rebuild )) || [[ ! -x $BIN || $SRC -nt $BIN ]]; then
    command -v "$HIPCC" > /dev/null || die "$HIPCC not found (set HIPCC=...)"
    arch=${OFFLOAD_ARCH:-$(rocminfo 2> /dev/null \
        | awk '/^ *Name: *gfx[0-9a-f]+ *$/ {print $2}' | sort -u | paste -sd,)}
    [[ -n $arch ]] || die "cannot detect GPU arch; set OFFLOAD_ARCH=gfx942"
    mkdir -p "$BUILD_DIR"
    arch_flags=()
    IFS=, read -ra archs <<< "$arch"
    for a in "${archs[@]}"; do arch_flags+=("--offload-arch=$a"); done
    printf 'Building %s for %s\n' "${BIN#"$SCRIPT_DIR"/}" "$arch" >&2
    "$HIPCC" -O2 -std=c++17 "${arch_flags[@]}" -o "$BIN" "$SRC" -lpthread
fi

if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    "$BIN" --help | sed 1,2d
    exit 0
fi

exec "$BIN" "$@"
