#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

MOCK_TREE="${TEMP_DIR}/debug_scripts"
OUTPUT_DIR="${TEMP_DIR}/bundle"
mkdir -p "$MOCK_TREE"
cp "${SCRIPT_DIR}/06_collect_all_gpu_debug.sh" "$MOCK_TREE/"
cp "${SCRIPT_DIR}/02_umr_queue_doctor.py" "$MOCK_TREE/"

for collector in \
  00_pull_build_umr.sh \
  03_dump_sdma_registers.sh \
  04_dump_kfd_snapshots.sh \
  05_dump_all_cpc_info.sh \
  08_dump_all_gpu_waves.sh \
  13_dump_aql_queues_gdb.sh; do
  cat >"${MOCK_TREE}/${collector}" <<'EOF'
#!/usr/bin/env bash
printf 'mock collector: %s\n' "$0"
printf 'mock args:'
printf ' %q' "$@"
printf '\n'
printf 'mock AMD_SMI: %q\n' "${AMD_SMI:-}"
case "$(basename "$0")" in
  05_dump_all_cpc_info.sh)
    exit 17
    ;;
  13_dump_aql_queues_gdb.sh)
    exit 124
    ;;
esac
EOF
  chmod +x "${MOCK_TREE}/${collector}"
done

MOCK_BIN="${TEMP_DIR}/bin"
mkdir -p "$MOCK_BIN"
cat >"${MOCK_BIN}/python3" <<'EOF'
#!/usr/bin/env bash
if printf '%s\n' "$*" | grep -q -- '--inventory-only'; then
  printf '%s\n' \
    '===== Queue Doctor Target GPUs =====' \
    '  DUMP AMD-SMI=4 / KFD node=6 / gpu_id=61984' \
    '===== End Queue Doctor Target GPUs ====='
  printf '%s\n' 'mock inventory warning' >&2
  exit "${MOCK_INVENTORY_RC:-0}"
else
  printf 'mock queue doctor args:'
  printf ' %q' "$@"
  printf '\n'
fi
EOF
chmod +x "${MOCK_BIN}/python3"

cat >"${MOCK_BIN}/amd-smi" <<'EOF'
#!/usr/bin/env bash
echo "mock amd-smi output from PATH"
EOF
chmod +x "${MOCK_BIN}/amd-smi"

cat >"${MOCK_BIN}/modinfo" <<'EOF'
#!/usr/bin/env bash
if [[ "$1" == "-F" && $# -eq 3 ]]; then
  printf 'mock-%s-%s\n' "$3" "$2"
  exit 0
fi
exit 2
EOF
chmod +x "${MOCK_BIN}/modinfo"

cat >"${MOCK_BIN}/dkms" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "status" ]] || exit 2
printf '%s\n' \
  'amdgpu/6.12.12-2199800.24.04, 6.8.0-31-generic, x86_64: installed' \
  'amdgpu/6.12.12-2199800.24.04, 6.8.0-32-generic, x86_64: built'
EOF
chmod +x "${MOCK_BIN}/dkms"

EXPLICIT_AMD_SMI_DIR="${TEMP_DIR}/explicit rocm/bin"
mkdir -p "$EXPLICIT_AMD_SMI_DIR"
EXPLICIT_AMD_SMI="${EXPLICIT_AMD_SMI_DIR}/amd-smi"
cat >"$EXPLICIT_AMD_SMI" <<'EOF'
#!/usr/bin/env bash
echo "mock explicit amd-smi output"
EOF
chmod +x "$EXPLICIT_AMD_SMI"

stdout_file="${TEMP_DIR}/stdout"
set +e
MOCK_INVENTORY_RC=23 AMD_SMI="$EXPLICIT_AMD_SMI" PATH="${MOCK_BIN}:$PATH" \
  bash "${MOCK_TREE}/06_collect_all_gpu_debug.sh" \
  --pid "$$" --gpu-id 0 --full-ring --keep-full-ring-raw \
  --update-umr --no-archive \
  --samples 1 --interval 1 --output-dir "$OUTPUT_DIR" \
  >"$stdout_file" 2>"${TEMP_DIR}/stderr"
collector_rc=$?
set -e

if ((collector_rc != 2)); then
  echo "expected collector rc=2, got rc=${collector_rc}" >&2
  exit 1
fi

if grep -q '===== Queue Doctor Target GPUs =====' "$stdout_file"; then
  echo "target inventory leaked to collector stdout" >&2
  exit 1
fi
if grep -q 'queue_doctor_target_inventory' "$stdout_file"; then
  echo "target inventory timing section leaked to collector stdout" >&2
  exit 1
fi
if grep -q 'DUMP AMD-SMI=' "$stdout_file"; then
  echo "target GPU detail leaked to collector stdout" >&2
  exit 1
fi
grep -q 'mock inventory warning' "${TEMP_DIR}/stderr"
grep -q '===== Queue Doctor Target GPUs =====' "${OUTPUT_DIR}/summary.txt"
grep -q '===== Queue Doctor Target GPUs =====' "${OUTPUT_DIR}/collector.txt"
grep -q -- '--full-ring' "${OUTPUT_DIR}/collector.txt"
grep -q 'mock queue doctor args:.*--full-ring' "${OUTPUT_DIR}/collector.txt"
grep -q 'mock queue doctor args:.*--keep-full-ring-raw' \
  "${OUTPUT_DIR}/collector.txt"
test -f "${OUTPUT_DIR}/commands.txt"
test -f "${OUTPUT_DIR}/system_info.txt"
grep -q '^===== loaded GPU driver modules (sysfs) =====$' \
  "${OUTPUT_DIR}/system_info.txt"
grep -q '^===== GPU driver module metadata (modinfo) =====$' \
  "${OUTPUT_DIR}/system_info.txt"
grep -q '^filename=mock-amdgpu-filename$' "${OUTPUT_DIR}/system_info.txt"
grep -q '^srcversion=mock-amdgpu-srcversion$' "${OUTPUT_DIR}/system_info.txt"
grep -q '^vermagic=mock-amdgpu-vermagic$' "${OUTPUT_DIR}/system_info.txt"
grep -q '^===== DKMS status =====$' "${OUTPUT_DIR}/system_info.txt"
grep -q '^amdgpu/6.12.12-2199800.24.04, 6.8.0-31-generic, x86_64: installed$' \
  "${OUTPUT_DIR}/system_info.txt"
grep -Fq "executable=${EXPLICIT_AMD_SMI}" "${OUTPUT_DIR}/system_info.txt"
grep -q '^mock explicit amd-smi output$' "${OUTPUT_DIR}/system_info.txt"
grep -Fq "mock AMD_SMI: ${EXPLICIT_AMD_SMI// /\\ }" \
  "${OUTPUT_DIR}/collector.txt"
if compgen -G "${OUTPUT_DIR}/*.log" >/dev/null; then
  echo "collector generated a stale .log artifact" >&2
  exit 1
fi

DISCARD_OUTPUT_DIR="${TEMP_DIR}/discard-bundle"
set +e
PATH="${MOCK_BIN}:$PATH" \
  bash "${MOCK_TREE}/06_collect_all_gpu_debug.sh" \
  --pid "$$" --gpu-id 0 --discard-full-ring-raw --no-archive \
  --samples 1 --interval 1 --output-dir "$DISCARD_OUTPUT_DIR" \
  --skip-sdma --skip-kfd --skip-cpc --skip-waves --skip-aql-queue \
  >"${TEMP_DIR}/discard-stdout" 2>"${TEMP_DIR}/discard-stderr"
discard_rc=$?
set -e
if ((discard_rc != 0)); then
  echo "expected discard collector rc=0, got rc=${discard_rc}" >&2
  exit 1
fi
grep -q 'mock queue doctor args:.*--discard-full-ring-raw' \
  "${DISCARD_OUTPUT_DIR}/collector.txt"
if grep -q 'mock queue doctor args:.*--keep-full-ring-raw' \
  "${DISCARD_OUTPUT_DIR}/collector.txt"; then
  echo "discard route also forwarded the keep flag" >&2
  exit 1
fi
grep -Fq "executable=${MOCK_BIN}/amd-smi" \
  "${DISCARD_OUTPUT_DIR}/system_info.txt"
grep -q '^mock amd-smi output from PATH$' \
  "${DISCARD_OUTPUT_DIR}/system_info.txt"

assert_step_elapsed() {
  local step="$1"
  local expected_status="$2"
  local expected_rc="$3"
  local bundle_dir="${4:-$OUTPUT_DIR}"
  local summary_row
  local summary_step
  local summary_status
  local summary_rc
  local summary_elapsed
  local log_elapsed

  summary_row="$(
    awk -F '\t' -v step="$step" '$1 == step { print; exit }' \
      "${bundle_dir}/summary.txt"
  )"
  if [[ -z "$summary_row" ]]; then
    echo "missing summary row for ${step}" >&2
    exit 1
  fi
  IFS=$'\t' read -r summary_step summary_status summary_rc summary_elapsed \
    <<<"$summary_row"
  if [[ "$summary_status" != "$expected_status" \
    || "$summary_rc" != "$expected_rc" ]]; then
    echo "unexpected summary status for ${step}: ${summary_row}" >&2
    exit 1
  fi
  if [[ ! "$summary_elapsed" =~ ^elapsed=[0-9]+s$ ]]; then
    echo "non-numeric summary elapsed for ${step}: ${summary_elapsed}" >&2
    exit 1
  fi

  log_elapsed="$(
    awk -v prefix="[collector] END ${step}:" '
      index($0, prefix) == 1 {
        for (field = 1; field <= NF; field++) {
          if ($field ~ /^elapsed=[0-9]+s$/) {
            print $field
            exit
          }
        }
      }
    ' "${bundle_dir}/collector.txt"
  )"
  if [[ "$log_elapsed" != "$summary_elapsed" ]]; then
    echo "elapsed mismatch for ${step}: log=${log_elapsed} summary=${summary_elapsed}" >&2
    exit 1
  fi
}

assert_step_elapsed update_build_umr PASS 0
assert_step_elapsed queue_doctor_target_inventory FAIL 23
assert_step_elapsed queue_doctor PASS 0
assert_step_elapsed sdma_registers PASS 0
assert_step_elapsed kfd_snapshots PASS 0
assert_step_elapsed cpc_dump FAIL 17
assert_step_elapsed wave_dump PASS 0
assert_step_elapsed aql_queue_dump FAIL 124

if grep '^\[collector\] END ' "${OUTPUT_DIR}/collector.txt" \
  | grep -Ev ' elapsed=[0-9]+s$'; then
  echo "collector END line missing numeric elapsed seconds" >&2
  exit 1
fi
if [[ ! "$(grep -c '^\[collector\] END ' "${OUTPUT_DIR}/collector.txt")" -eq 8 ]]; then
  echo "unexpected collector END line count" >&2
  exit 1
fi
grep -Eq '^\[collector\] TOTAL elapsed=[0-9]+s$' \
  "${OUTPUT_DIR}/collector.txt"
grep -Eq '^total_elapsed=[0-9]+s$' "${OUTPUT_DIR}/summary.txt"
grep -q $'^step\tstatus\trc_or_reason\telapsed$' \
  "${OUTPUT_DIR}/summary.txt"
if grep -q -- '^--full-ring: command not found' \
  "${OUTPUT_DIR}/collector.txt"; then
  echo "full-ring was executed as a standalone command" >&2
  exit 1
fi

EARLY_OUTPUT_DIR="${TEMP_DIR}/early-return-bundle"
set +e
PATH="${MOCK_BIN}:$PATH" \
  bash "${MOCK_TREE}/06_collect_all_gpu_debug.sh" \
  --pid 999999999 --no-archive --skip-queue --skip-sdma --skip-kfd \
  --skip-cpc --skip-waves --output-dir "$EARLY_OUTPUT_DIR" \
  >"${TEMP_DIR}/early-stdout" 2>"${TEMP_DIR}/early-stderr"
early_rc=$?
set -e
if ((early_rc != 2)); then
  echo "expected early-return collector rc=2, got rc=${early_rc}" >&2
  exit 1
fi
assert_step_elapsed aql_queue_dump FAIL 3 "$EARLY_OUTPUT_DIR"

if [[ -x /opt/rocm/bin/amd-smi ]]; then
  STANDARD_OUTPUT_DIR="${TEMP_DIR}/standard-rocm-bundle"
  PATH=/usr/bin:/bin AMD_SMI=/missing/configured/amd-smi \
    bash "${MOCK_TREE}/06_collect_all_gpu_debug.sh" \
    --pid "$$" --no-archive --skip-queue --skip-sdma --skip-kfd \
    --skip-cpc --skip-waves --skip-aql-queue \
    --output-dir "$STANDARD_OUTPUT_DIR" \
    >"${TEMP_DIR}/standard-stdout" 2>"${TEMP_DIR}/standard-stderr"
  grep -q '^executable=/opt/rocm/bin/amd-smi$' \
    "${STANDARD_OUTPUT_DIR}/system_info.txt"
fi

echo "mocked collector routing test passed"
