#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd)"
DUMP_SCRIPT="${ROOT_DIR}/03_dump_sdma_registers.sh"
FAKE_UMR="${SCRIPT_DIR}/fake_umr_sdma_stub.sh"
TEST_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/sdma_dump_test.XXXXXX")"
TEST_BIN="${TEST_ROOT}/bin"

cleanup() {
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

chmod +x "$FAKE_UMR"
mkdir -p "$TEST_BIN"
cat >"${TEST_BIN}/sudo" <<'EOF'
#!/usr/bin/env bash
exec "$@"
EOF
chmod +x "${TEST_BIN}/sudo"
export PATH="${TEST_BIN}:${PATH}"

run_case() {
  local name="$1"
  shift
  echo "== case: $name =="
  "$@"
}

assert_grep() {
  local pattern="$1"
  local file="$2"
  if ! grep -qE -- "$pattern" "$file"; then
    echo "ASSERT FAILED: expected '$pattern' in $file" >&2
    exit 1
  fi
}

assert_not_grep() {
  local pattern="$1"
  local file="$2"
  if grep -qE -- "$pattern" "$file"; then
    echo "ASSERT FAILED: did not expect '$pattern' in $file" >&2
    exit 1
  fi
}

bash -n "$DUMP_SCRIPT"
"$DUMP_SCRIPT" --help >/dev/null

# Version gate: 1.0.10 must fail before any reads.
if ! FAKE_UMR_VERSION=1.0.10 UMR="$FAKE_UMR" AMD_SMI= DMESG= \
  "$DUMP_SCRIPT" -o "$TEST_ROOT/old_umr.log" >/dev/null 2>&1; then
  :
else
  echo "ASSERT FAILED: UMR 1.0.10 should fail precheck" >&2
  exit 1
fi
assert_grep 'below required >= 1.0.11' "$TEST_ROOT/old_umr.log"

# Baseline sequential fake run.
LOG="$TEST_ROOT/seq.log"
FAKE_UMR_VERSION=1.0.11 \
FAKE_UMR_INSTANCES="1 2 3" \
FAKE_UMR_BLOCKS_PER_GPU=2 \
UMR="$FAKE_UMR" AMD_SMI= DMESG= \
  "$DUMP_SCRIPT" --jobs 1 -o "$LOG"
assert_grep 'umr_read_strategy=one_batched_read_per_gpu' "$LOG"
assert_grep 'umr_lb_invocations=3' "$LOG"
assert_grep 'umr_read_invocations=3' "$LOG"
assert_grep 'umr_total_invocations=6' "$LOG"
assert_grep 'total_reads=606' "$LOG"
assert_grep 'block_summary: total=101 successful=101 failed=0 umr_rc=0' "$LOG"

# Parallel jobs should preserve totals and deterministic GPU order in merge.
LOG="$TEST_ROOT/parallel.log"
FAKE_UMR_VERSION=1.0.11 \
FAKE_UMR_INSTANCES="1 2 3" \
FAKE_UMR_BLOCKS_PER_GPU=2 \
FAKE_UMR_SLEEP_SEC=0 \
UMR="$FAKE_UMR" AMD_SMI= DMESG= \
  "$DUMP_SCRIPT" --jobs 3 -o "$LOG"
assert_grep 'parallel_jobs=3' "$LOG"
assert_grep 'total_reads=606' "$LOG"
first_gpu_line="$(grep -n '=== gpu=.* list-blocks ===' "$LOG" | head -1 | cut -d: -f1)"
second_gpu_line="$(grep -n '=== gpu=.* list-blocks ===' "$LOG" | sed -n '2p' | cut -d: -f1)"
third_gpu_line="$(grep -n '=== gpu=.* list-blocks ===' "$LOG" | sed -n '3p' | cut -d: -f1)"
if (( first_gpu_line >= second_gpu_line || second_gpu_line >= third_gpu_line )); then
  echo "ASSERT FAILED: GPU sections not in deterministic order" >&2
  exit 1
fi

# One failing GPU instance should not stop others.
LOG="$TEST_ROOT/fail_gpu.log"
FAKE_UMR_VERSION=1.0.11 \
FAKE_UMR_INSTANCES="1 2 3" \
FAKE_UMR_BLOCKS_PER_GPU=2 \
FAKE_UMR_FAIL_INSTANCE=2 \
UMR="$FAKE_UMR" AMD_SMI= DMESG= \
  "$DUMP_SCRIPT" --jobs 2 -o "$LOG" || rc=$?
rc="${rc:-0}"
if (( rc == 0 )); then
  echo "ASSERT FAILED: expected non-zero exit when one GPU fails discovery/read" >&2
  exit 1
fi
assert_grep 'discovery_failures=1' "$LOG"
assert_grep 'total_reads=404' "$LOG"

# Timeout on hung UMR read.
LOG="$TEST_ROOT/timeout.log"
FAKE_UMR_VERSION=1.0.11 \
FAKE_UMR_INSTANCES="1" \
FAKE_UMR_BLOCKS_PER_GPU=1 \
FAKE_UMR_HANG_READ_SEC=30 \
UMR="$FAKE_UMR" AMD_SMI= DMESG= \
  "$DUMP_SCRIPT" --jobs 1 --timeout 2 -o "$LOG" || rc=$?
rc="${rc:-0}"
if (( rc == 0 )); then
  echo "ASSERT FAILED: expected non-zero exit on timeout" >&2
  exit 1
fi
assert_grep 'umr_timed_out=1' "$LOG"
assert_grep 'timed out after 2s' "$LOG"

echo "All fake UMR SDMA dump tests passed."
