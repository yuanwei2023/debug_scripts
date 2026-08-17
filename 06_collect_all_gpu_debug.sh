#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added unified collection for GPU debug artifacts.
#   2026-08-03 - Added dedicated all-GPU wave snapshots under waves/.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_ARGS=("$@")
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
PID=""
QUEUE_PIDS=()
GPU_IDS=()
NODE_IDS=()
USE_ALL_GPUS=1
SAMPLES=3
INTERVAL=2
VMP=1
OUTPUT_DIR=""
UPDATE_UMR=0
FULL_RING=0
DYNAMIC_DEBUG=0
SKIP_QUEUE=0
SKIP_SDMA=0
SKIP_KFD=0
SKIP_CPC=0
SKIP_WAVES=0
SKIP_AQL_QUEUE=0
CREATE_ARCHIVE=1

usage() {
  cat <<'EOF'
Usage: ./06_collect_all_gpu_debug.sh --pid PID [options]

Run the GPU debug collectors sequentially and package their artifacts into one
timestamped directory. --pid is required for Queue Doctor and AQL queue dump.

Options:
  --pid PID             Target process ID (required)
  --gpu-id ID           AMD-SMI GPU ID; may be repeated (default: all PID GPUs)
  --node-id ID          KFD topology node ID; may be repeated
  --samples N           Queue/KFD snapshot count (default: 3)
  --interval SEC        Seconds between snapshots (default: 2)
  --vmp N               UMR VM partition (default: 1)
  -o, --output-dir DIR  Bundle directory (default: /tmp/debug-tools-<UID>)
  --update-umr          Pull and build UMR before collecting
  --full-ring           Include complete retained rings in Queue Doctor
  --dynamic-debug       Temporarily enable KFD dynamic debug
  --skip-queue          Skip Queue Doctor
  --skip-sdma           Skip the SDMA register dump
  --skip-kfd            Skip KFD snapshots
  --skip-cpc            Skip CPC/system-state collection
  --skip-waves          Skip dedicated all-GPU wave snapshots
  --skip-aql-queue      Skip AQL/SDMA gdb queue dump (13_dump_aql_queues_gdb.sh)
  --no-archive          Do not create the final .tgz archive
  -h, --help            Show this help

Examples:
  ./06_collect_all_gpu_debug.sh --pid 4008481
  ./06_collect_all_gpu_debug.sh --pid 4008481 --gpu-id 0 --vmp 2

Important:
  CPC collection enables amdgpu halt_if_hws_hang and captures waves.

Environment:
  DEBUG_TOOLS_TMP_DIR  Default temporary output root
  GDB                  Optional GDB for script 13 (default: /usr/bin/gdb)
  GDB_TIMEOUT          Attach/decode timeout for AQL queue dump (default: 120)
  LD_PRELOAD           Optional; use when invoking 06 if script 13 needs it
EOF
}

require_value() {
  if (($# < 2)) || [[ -z "$2" ]]; then
    echo "Option $1 requires a value" >&2
    exit 2
  fi
}

while (($# > 0)); do
  case "$1" in
    --pid=*)
      PID="${1#*=}"
      shift
      ;;
    --pid)
      require_value "$@"
      PID="$2"
      shift 2
      ;;
    --gpu-id)
      require_value "$@"
      GPU_IDS+=("$2")
      USE_ALL_GPUS=0
      shift 2
      ;;
    --node-id)
      require_value "$@"
      NODE_IDS+=("$2")
      USE_ALL_GPUS=0
      shift 2
      ;;
    --samples)
      require_value "$@"
      SAMPLES="$2"
      shift 2
      ;;
    --interval)
      require_value "$@"
      INTERVAL="$2"
      shift 2
      ;;
    --vmp)
      require_value "$@"
      VMP="$2"
      shift 2
      ;;
    -o|--output-dir)
      require_value "$@"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --update-umr)
      UPDATE_UMR=1
      shift
      ;;
    --full-ring)
      FULL_RING=1
      shift
      ;;
    --dynamic-debug)
      DYNAMIC_DEBUG=1
      shift
      ;;
    --skip-queue)
      SKIP_QUEUE=1
      shift
      ;;
    --skip-sdma)
      SKIP_SDMA=1
      shift
      ;;
    --skip-kfd)
      SKIP_KFD=1
      shift
      ;;
    --skip-cpc)
      SKIP_CPC=1
      shift
      ;;
    --skip-waves)
      SKIP_WAVES=1
      shift
      ;;
    --skip-aql-queue)
      SKIP_AQL_QUEUE=1
      shift
      ;;
    --no-archive)
      CREATE_ARCHIVE=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$PID" ]]; then
  echo "--pid is required" >&2
  usage >&2
  exit 2
fi
if [[ ! "$PID" =~ ^[1-9][0-9]*$ ]]; then
  echo "PID must be a positive integer: ${PID}" >&2
  exit 2
fi
if [[ ! "$SAMPLES" =~ ^[1-9][0-9]*$ ]]; then
  echo "Samples must be a positive integer: ${SAMPLES}" >&2
  exit 2
fi
if [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "Interval must be a positive integer: ${INTERVAL}" >&2
  exit 2
fi
if [[ ! "$VMP" =~ ^[0-9]+$ ]]; then
  echo "VMP must be a non-negative integer: ${VMP}" >&2
  exit 2
fi
if ((${#GPU_IDS[@]} > 0 && ${#NODE_IDS[@]} > 0)); then
  echo "--gpu-id and --node-id cannot be used together" >&2
  exit 2
fi
for id in "${GPU_IDS[@]}" "${NODE_IDS[@]}"; do
  if [[ ! "$id" =~ ^[0-9]+$ ]]; then
    echo "GPU and node IDs must be non-negative integers: ${id}" >&2
    exit 2
  fi
done

for script in \
  00_pull_build_umr.sh \
  02_umr_queue_doctor.py \
  03_dump_sdma_registers.sh \
  04_dump_kfd_snapshots.sh \
  05_dump_all_cpc_info.sh \
  08_dump_all_gpu_waves.sh \
  13_dump_aql_queues_gdb.sh; do
  if [[ ! -e "${SCRIPT_DIR}/${script}" ]]; then
    echo "Required collector is missing: ${SCRIPT_DIR}/${script}" >&2
    exit 1
  fi
done

if ((EUID != 0)); then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required for GPU debugfs collection" >&2
    exit 1
  fi
  echo "Validating sudo credentials..."
  if ! sudo -v; then
    echo "sudo authentication failed" >&2
    exit 1
  fi
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
hostname_short="$(hostname -s 2>/dev/null || hostname)"
if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="${DEFAULT_LOG_ROOT}/gpu_debug_bundle_${hostname_short}_${timestamp}"
fi
if [[ -e "$OUTPUT_DIR" ]]; then
  echo "Output path already exists: ${OUTPUT_DIR}" >&2
  exit 1
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd)"
QUEUE_PIDS=("$PID")

RUN_LOG="${OUTPUT_DIR}/collector.log"
SUMMARY="${OUTPUT_DIR}/summary.txt"
COMMANDS="${OUTPUT_DIR}/commands.log"
SYSTEM_INFO="${OUTPUT_DIR}/system_info.log"
ARCHIVE="${OUTPUT_DIR}.tgz"
failures=0
steps_run=0
steps_skipped=0
LAST_STEP_RC=0

: >"$RUN_LOG"
: >"$SUMMARY"
: >"$COMMANDS"

log() {
  printf '[collector] %s\n' "$*" | tee -a "$RUN_LOG"
}

highlight_path() {
  local label="$1"
  local path="$2"
  local color_header=""
  local color_path=""
  local color_reset=""
  local separator="============================================================"

  if [[ -t 1 ]]; then
    color_header=$'\033[1;32m'
    color_path=$'\033[1;33m'
    color_reset=$'\033[0m'
  fi

  printf '\n%b%s%b\n' "$color_header" "$separator" "$color_reset"
  printf '%b  %s%b\n' "$color_header" "$label" "$color_reset"
  printf '%b  %s%b\n' "$color_path" "$path" "$color_reset"
  printf '%b%s%b\n\n' "$color_header" "$separator" "$color_reset"
  printf '[collector] %s: %s\n' "$label" "$path" >>"$RUN_LOG"
}

highlight_archive() {
  highlight_path "LOG ARCHIVE TO SEND" "$ARCHIVE"
}

highlight_path "SAVING LOGS TO" "$OUTPUT_DIR"

record_command() {
  local step="$1"
  shift
  {
    printf 'step=%q command=' "$step"
    printf '%q ' "$@"
    printf '\n'
  } >>"$COMMANDS"
}

record_result() {
  local step="$1"
  local status="$2"
  local rc="$3"
  printf '%s\t%s\t%s\n' "$step" "$status" "$rc" >>"$SUMMARY"
}

run_step() {
  local step="$1"
  shift
  local rc

  steps_run=$((steps_run + 1))
  record_command "$step" "$@"
  log "BEGIN ${step}"
  "$@" >>"$RUN_LOG" 2>&1
  rc=$?
  LAST_STEP_RC=$rc
  if ((rc == 0)); then
    record_result "$step" "PASS" "$rc"
    log "END ${step}: PASS"
  else
    failures=$((failures + 1))
    record_result "$step" "FAIL" "$rc"
    log "END ${step}: FAIL rc=${rc}; continuing"
  fi
  return 0
}

skip_step() {
  local step="$1"
  local reason="$2"
  steps_skipped=$((steps_skipped + 1))
  record_result "$step" "SKIP" "$reason"
  log "SKIP ${step}: ${reason}"
}

fail_step() {
  local step="$1"
  local reason="$2"
  steps_run=$((steps_run + 1))
  failures=$((failures + 1))
  log "BEGIN ${step}"
  record_result "$step" "FAIL" "$reason"
  log "END ${step}: FAIL reason=${reason}; continuing"
}

run_aql_queue_dump() {
  local target_pid="$1"
  local out_dir="$2"
  local dump_script="${SCRIPT_DIR}/13_dump_aql_queues_gdb.sh"

  if [[ ! -d "/proc/${target_pid}" ]]; then
    echo "PID ${target_pid} is not running; skipping AQL queue dump" >&2
    return 3
  fi

  mkdir -p "$out_dir"
  echo "AQL queue dump PID=${target_pid} output=${out_dir}"
  GDB_TIMEOUT="${GDB_TIMEOUT:-120}" \
    "${dump_script}" --pid "${target_pid}" --output-dir "${out_dir}"
}

{
  echo "collected_at=$(date --iso-8601=seconds)"
  echo "hostname=$(hostname)"
  echo "kernel=$(uname -r)"
  printf 'command_line='
  printf '%q ' "$0" "${ORIGINAL_ARGS[@]}"
  printf '\n'
  echo
  echo "===== uname ====="
  uname -a
  echo
  echo "===== uptime ====="
  uptime
  echo
  echo "===== memory ====="
  free -h 2>&1 || true
  echo
  echo "===== PCI display devices ====="
  lspci -nn 2>&1 | grep -Ei 'VGA|Display|AMD|ATI' || true
  echo
  echo "===== amd-smi ====="
  if command -v amd-smi >/dev/null 2>&1; then
    amd-smi 2>&1 || true
  else
    echo "amd-smi not found"
  fi
  echo
  echo "===== UMR revision ====="
  if [[ -d /home/weiyuan2/umr/.git ]]; then
    git -C /home/weiyuan2/umr status --short --branch 2>&1 || true
    git -C /home/weiyuan2/umr log -1 --oneline 2>&1 || true
  else
    echo "/home/weiyuan2/umr is not a Git checkout"
  fi
} >"$SYSTEM_INFO"

{
  echo -e "step\tstatus\trc_or_reason"
} >"$SUMMARY"

if ((UPDATE_UMR == 1)); then
  run_step "update_build_umr" "${SCRIPT_DIR}/00_pull_build_umr.sh"
else
  skip_step "update_build_umr" "not-requested"
fi

if ((SKIP_QUEUE == 1)); then
  skip_step "queue_doctor" "disabled"
else
  if ((${#QUEUE_PIDS[@]} == 0)); then
    fail_step "queue_doctor" "no-target-pid"
  fi

  for queue_pid in "${QUEUE_PIDS[@]}"; do
    queue_step="queue_doctor"
    queue_output="${OUTPUT_DIR}/queue_doctor"
    if ((${#QUEUE_PIDS[@]} > 1)); then
      queue_step="queue_doctor_pid${queue_pid}"
      queue_output="${OUTPUT_DIR}/queue_doctor_pid${queue_pid}"
    fi

    queue_command=(
      python3 "${SCRIPT_DIR}/02_umr_queue_doctor.py"
      --pid "$queue_pid"
      --vm-partition "$VMP"
      --samples "$SAMPLES"
      --interval "$INTERVAL"
      --output-dir "$queue_output"
    )
    if ((${#GPU_IDS[@]} > 0)); then
      queue_command+=(--gpu-id "${GPU_IDS[@]}")
    elif ((${#NODE_IDS[@]} > 0)); then
      queue_command+=(--node-id "${NODE_IDS[@]}")
    elif ((USE_ALL_GPUS == 1)); then
      queue_command+=(--all-gpus)
    fi
    if ((FULL_RING == 1)); then
      queue_command+=(--full-ring)
    fi
    run_step "$queue_step" "${queue_command[@]}"
  done
fi

if ((SKIP_AQL_QUEUE == 1)); then
  skip_step "aql_queue_dump" "disabled"
else
  if ((${#QUEUE_PIDS[@]} == 0)); then
    fail_step "aql_queue_dump" "no-target-pid"
  else
    for queue_pid in "${QUEUE_PIDS[@]}"; do
      aql_step="aql_queue_dump"
      aql_output="${OUTPUT_DIR}/aql_queue_dump"
      if ((${#QUEUE_PIDS[@]} > 1)); then
        aql_step="aql_queue_dump_pid${queue_pid}"
        aql_output="${OUTPUT_DIR}/aql_queue_dump_pid${queue_pid}"
      fi
      run_step "$aql_step" run_aql_queue_dump "$queue_pid" "$aql_output"
    done
  fi
fi

if ((SKIP_SDMA == 1)); then
  skip_step "sdma_registers" "disabled"
else
  run_step "sdma_registers" \
    "${SCRIPT_DIR}/03_dump_sdma_registers.sh" \
    --vmp "$VMP" --output "${OUTPUT_DIR}/sdma_registers.log"
fi

if ((SKIP_KFD == 1)); then
  skip_step "kfd_snapshots" "disabled"
else
  kfd_command=(
    "${SCRIPT_DIR}/04_dump_kfd_snapshots.sh"
    --snapshots "$SAMPLES"
    --interval "$INTERVAL"
    --output-dir "${OUTPUT_DIR}/kfd"
  )
  if ((DYNAMIC_DEBUG == 1)); then
    kfd_command+=(--dynamic-debug)
  fi
  run_step "kfd_snapshots" "${kfd_command[@]}"
fi

if ((SKIP_CPC == 1)); then
  skip_step "cpc_dump" "disabled"
else
  run_step "cpc_dump" \
    "${SCRIPT_DIR}/05_dump_all_cpc_info.sh" \
    --output "${OUTPUT_DIR}/cpc"
fi

if ((SKIP_WAVES == 1)); then
  skip_step "wave_dump" "disabled"
else
  run_step "wave_dump" \
    "${SCRIPT_DIR}/08_dump_all_gpu_waves.sh" \
    --output "${OUTPUT_DIR}/waves" \
    --no-archive
fi

{
  echo
  echo "bundle_dir=${OUTPUT_DIR}"
  echo "archive=${ARCHIVE}"
  echo "steps_run=${steps_run}"
  echo "steps_skipped=${steps_skipped}"
  echo "failures=${failures}"
  echo "completed_at=$(date --iso-8601=seconds)"
} >>"$SUMMARY"

if ((CREATE_ARCHIVE == 1)); then
  log "Creating archive: ${ARCHIVE}"
  if ! tar -czf "$ARCHIVE" \
    -C "$(dirname "$OUTPUT_DIR")" "$(basename "$OUTPUT_DIR")"; then
    failures=$((failures + 1))
    log "Archive creation failed"
  fi
else
  log "Archive creation disabled"
fi

echo "final_failures=${failures}" >>"$SUMMARY"
log "Collection complete: ${OUTPUT_DIR}"
if ((CREATE_ARCHIVE == 1)) && [[ -f "$ARCHIVE" ]]; then
  log "Archive: ${ARCHIVE}"
fi
if ((failures > 0)); then
  log "Completed with ${failures} failed step(s); see ${SUMMARY}"
fi
if ((CREATE_ARCHIVE == 1)) && [[ -f "$ARCHIVE" ]]; then
  highlight_archive
fi
if ((failures > 0)); then
  exit 2
fi
