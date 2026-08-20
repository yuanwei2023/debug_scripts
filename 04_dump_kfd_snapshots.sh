#!/usr/bin/env bash
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added bounded KFD, AMD-SMI, fence, trace, and dmesg snapshots.
#
# Collect bounded KFD, fence, AMD-SMI, trace, and dmesg snapshots.
# All artifacts are stored under one timestamped run directory.

set -Eeuo pipefail

SNAPSHOTS="${SNAPSHOTS:-3}"
INTERVAL_SEC="${INTERVAL_SEC:-20}"

READ_TIMEOUT_SEC="${READ_TIMEOUT_SEC:-120}"
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
OUT_BASE="${OUT_BASE:-${DEFAULT_LOG_ROOT}}"
KFD_DIR="${KFD_DIR:-/sys/kernel/debug/kfd}"
DRI_DIR="${DRI_DIR:-/sys/kernel/debug/dri}"
DYNDBG_CTL="${DYNDBG_CTL:-/sys/kernel/debug/dynamic_debug/control}"
TRACE_ROOT="${TRACE_ROOT:-}"
AMD_SMI="${AMD_SMI:-}"
DMESG="${DMESG:-$(command -v dmesg 2>/dev/null || true)}"
ENABLE_DYNAMIC_DEBUG="${ENABLE_DYNAMIC_DEBUG:-0}"
ENABLE_TRACE=1
ENABLE_FENCE=1
ENABLE_DMESG=1
KFD_FILES=(rls hqds mqds)
TRACE_EVENTS=(
  gpu_scheduler/drm_sched_job_queue
  gpu_scheduler/drm_sched_job_run
  gpu_scheduler/drm_sched_job_done
  dma_fence/dma_fence_init
  dma_fence/dma_fence_signaled
  dma_fence/dma_fence_wait_start
  dma_fence/dma_fence_wait_end
)
DYNDBG_FILES=(
  kfd_device_queue_manager.c
  kfd_packet_manager.c
  kfd_packet_manager_v9.c
)

usage() {
  cat <<'USAGE'
Usage: 04_dump_kfd_snapshots.sh [options]

Collect finite KFD queue snapshots plus AMD-SMI, fence, trace, and dmesg data.
The script never follows trace_pipe or dmesg indefinitely.

Options:
  -n, --snapshots N       Number of snapshots (default: 3)
  -i, --interval SEC      Seconds between snapshots (default: 20)
  -o, --output-dir DIR    Base output directory (default: /tmp/debug-tools-<UID>)
      --timeout SEC       Per-command timeout seconds (default: 120)
      --dynamic-debug     Temporarily enable KFD dynamic debug (default: off)
      --no-dynamic-debug  Keep KFD dynamic debug disabled (compatibility option)
      --no-trace          Do not collect drm_sched/dma_fence trace events
      --no-fence          Do not collect amdgpu_fence_info or fence diffs
      --no-dmesg          Do not collect dmesg snapshots
  -h, --help              Show this help

Output:
  kfd-dump-<timestamp>/
    snapshot_<N>_<timestamp>/{rls,hqds,mqds,amd-smi,dmesg,fence_info}.txt
    trace/drm_sched_dma_fence.txt
    fence_diff.{full,filtered}.txt
    dmesg-final.txt
    manifest.txt

Environment overrides:
  DEBUG_TOOLS_TMP_DIR, OUT_BASE, KFD_DIR, DRI_DIR, DYNDBG_CTL, TRACE_ROOT,
  AMD_SMI, DMESG, ENABLE_DYNAMIC_DEBUG
USAGE
}

is_positive_integer() {
  [[ "$1" =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 ))
}

discover_amd_smi() {
  local configured="$AMD_SMI"
  local candidate

  # An explicitly configured executable wins, including paths containing
  # whitespace. If it is unusable, continue so a stale collector environment
  # does not hide an installed ROCm executable.
  if [[ -n "$configured" ]]; then
    if [[ -x "$configured" ]]; then
      printf '%s\n' "$configured"
      return 0
    fi
    candidate="$(command -v -- "$configured" 2>/dev/null || true)"
    if [[ -n "$candidate" && -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi

  candidate="$(command -v amd-smi 2>/dev/null || true)"
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  # sudo and non-interactive collectors commonly receive a restricted PATH.
  for candidate in \
    /opt/rocm/bin/amd-smi \
    /usr/local/bin/amd-smi \
    /usr/bin/amd-smi; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

while (( $# > 0 )); do
  case "$1" in
    -n|--snapshots)
      (( $# >= 2 )) || { echo "[kfd-dump] $1 requires a value" >&2; exit 1; }
      SNAPSHOTS="$2"
      shift 2
      ;;
    -i|--interval)
      (( $# >= 2 )) || { echo "[kfd-dump] $1 requires a value" >&2; exit 1; }
      INTERVAL_SEC="$2"
      shift 2
      ;;
    -o|--output-dir)
      (( $# >= 2 )) || { echo "[kfd-dump] $1 requires a value" >&2; exit 1; }
      OUT_BASE="$2"
      shift 2
      ;;
    --timeout)
      (( $# >= 2 )) || { echo "[kfd-dump] $1 requires a value" >&2; exit 1; }
      READ_TIMEOUT_SEC="$2"
      shift 2
      ;;
    --dynamic-debug)
      ENABLE_DYNAMIC_DEBUG=1
      shift
      ;;
    --no-dynamic-debug)
      ENABLE_DYNAMIC_DEBUG=0
      shift
      ;;
    --no-trace)
      ENABLE_TRACE=0
      shift
      ;;
    --no-fence)
      ENABLE_FENCE=0
      shift
      ;;
    --no-dmesg)
      ENABLE_DMESG=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[kfd-dump] unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

AMD_SMI="$(discover_amd_smi || true)"

for value_name in SNAPSHOTS INTERVAL_SEC READ_TIMEOUT_SEC; do
  value="${!value_name}"
  if ! is_positive_integer "$value"; then
    echo "[kfd-dump] invalid ${value_name}: ${value}" >&2
    exit 1
  fi
done

if [[ "$ENABLE_DYNAMIC_DEBUG" != "0" && "$ENABLE_DYNAMIC_DEBUG" != "1" ]]; then
  echo "[kfd-dump] invalid ENABLE_DYNAMIC_DEBUG: ${ENABLE_DYNAMIC_DEBUG} (expected 0 or 1)" >&2
  exit 1
fi

for command_name in date timeout find flock awk sort diff grep; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "[kfd-dump] missing command: $command_name" >&2
    exit 2
  fi
done

if (( EUID == 0 )) || [[ "${KFD_DUMP_NO_SUDO:-0}" == "1" ]]; then
  ROOT_CMD=()
else
  if ! command -v sudo >/dev/null 2>&1; then
    echo "[kfd-dump] root access is required and sudo is unavailable" >&2
    exit 2
  fi
  ROOT_CMD=(sudo)
fi

run_root() {
  "${ROOT_CMD[@]}" "$@"
}

write_root() {
  local path="$1"
  local value="$2"
  printf '%s\n' "$value" | run_root tee "$path" >/dev/null
}

mkdir -p "$OUT_BASE"
exec 9>"${OUT_BASE%/}/.kfd-dump.lock"
if ! flock -n 9; then
  echo "[kfd-dump] another collection is already running for ${OUT_BASE}" >&2
  exit 4
fi

RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${OUT_BASE%/}/kfd-dump-${RUN_TS}"
mkdir -p "$RUN_DIR"
RUN_LOG="${RUN_DIR}/run.txt"
WARN_LOG="${RUN_DIR}/warnings.txt"
ERROR_LOG="${RUN_DIR}/errors.txt"
: >"$RUN_LOG"
: >"$WARN_LOG"
: >"$ERROR_LOG"

FAILURES=0
WARNINGS=0
SUCCESSFUL_CAPTURES=0
TRACE_INSTANCE=""
TRACE_ACTIVE=0
DYNDBG_CHANGED=0
DYNDBG_BEFORE="${RUN_DIR}/dynamic_debug.before.txt"
SNAPSHOT_DIRS=()

now() {
  date '+%Y-%m-%dT%H:%M:%S.%3N%:z'
}

log() {
  printf '[%s] [kfd-dump] %s\n' "$(now)" "$*" | tee -a "$RUN_LOG"
}

warn() {
  WARNINGS=$((WARNINGS + 1))
  printf '[%s] [kfd-dump] WARNING: %s\n' "$(now)" "$*" | tee -a "$RUN_LOG" "$WARN_LOG" >&2
}

record_error() {
  FAILURES=$((FAILURES + 1))
  printf '[%s] [kfd-dump] ERROR: %s\n' "$(now)" "$*" | tee -a "$RUN_LOG" "$ERROR_LOG" >&2
}

capture_command() {
  local destination="$1"
  local label="$2"
  shift 2
  local rc=0

  if timeout --foreground "${READ_TIMEOUT_SEC}s" "$@" >"$destination" 2>&1; then
    SUCCESSFUL_CAPTURES=$((SUCCESSFUL_CAPTURES + 1))
    return 0
  else
    rc=$?
    record_error "${label} failed or timed out (rc=${rc}); output=${destination}"
    return 0
  fi
}

capture_root_file() {
  local source="$1"
  local destination="$2"
  local label="$3"

  if ! run_root test -e "$source"; then
    record_error "missing ${label}: ${source}"
    return 0
  fi
  capture_command "$destination" "$label" "${ROOT_CMD[@]}" cat "$source"
}

capture_dmesg() {
  local destination="$1"
  if (( ENABLE_DMESG == 0 )); then
    return 0
  fi
  if [[ -z "$DMESG" || ! -x "$DMESG" ]]; then
    warn "dmesg is unavailable"
    return 0
  fi
  capture_command "$destination" "dmesg" \
    "${ROOT_CMD[@]}" "$DMESG" --color=never --time-format iso
  grep -Ei 'amdgpu|amdkfd|kfd|drm_sched|dma_fence|ih_fifo|timeout|reset|fence' \
    "$destination" >"${destination%.txt}-filtered.txt" || true
}

capture_fences() {
  local destination="$1"
  local found=0
  local fence_file

  if (( ENABLE_FENCE == 0 )); then
    return 0
  fi
  if ! run_root test -d "$DRI_DIR"; then
    warn "DRI debugfs directory is unavailable: ${DRI_DIR}"
    return 0
  fi

  : >"$destination"
  while IFS= read -r fence_file; do
    [[ -n "$fence_file" ]] || continue
    found=$((found + 1))
    {
      printf '===== %s =====\n' "$fence_file"
      run_root cat "$fence_file" 2>&1 || true
      echo
    } >>"$destination"
  done < <(run_root find "$DRI_DIR" -type f -name amdgpu_fence_info -print 2>/dev/null | sort)

  if (( found == 0 )); then
    warn "no amdgpu_fence_info files found under ${DRI_DIR}"
  else
    SUCCESSFUL_CAPTURES=$((SUCCESSFUL_CAPTURES + 1))
  fi
}

enable_dynamic_debug() {
  local source_file

  (( ENABLE_DYNAMIC_DEBUG == 1 )) || return 0
  if ! run_root test -e "$DYNDBG_CTL"; then
    warn "dynamic-debug control is unavailable: ${DYNDBG_CTL}"
    return 0
  fi

  run_root cat "$DYNDBG_CTL" 2>/dev/null |
    grep -E 'amdkfd/(kfd_device_queue_manager|kfd_packet_manager(_v9)?)\.c' \
      >"$DYNDBG_BEFORE" || true

  for source_file in "${DYNDBG_FILES[@]}"; do
    if ! write_root "$DYNDBG_CTL" "file */amdkfd/${source_file} +p"; then
      warn "failed to enable dynamic debug for ${source_file}"
    fi
  done
  DYNDBG_CHANGED=1
  log "temporarily enabled KFD dynamic debug"
}

restore_dynamic_debug() {
  local source_file line flags location source_path line_number base_name

  (( DYNDBG_CHANGED == 1 )) || return 0
  DYNDBG_CHANGED=0

  for source_file in "${DYNDBG_FILES[@]}"; do
    write_root "$DYNDBG_CTL" "file */amdkfd/${source_file} -p" 2>/dev/null || true
  done

  if [[ -s "$DYNDBG_BEFORE" ]]; then
    while IFS= read -r line; do
      flags="$(awk '{print $3}' <<<"$line")"
      [[ "$flags" == *p* ]] || continue
      location="${line%% *}"
      source_path="${location%:*}"
      line_number="${location##*:}"
      base_name="${source_path##*/}"
      [[ "$line_number" =~ ^[0-9]+$ ]] || continue
      write_root "$DYNDBG_CTL" "file ${base_name} line ${line_number} +p" 2>/dev/null || true
    done <"$DYNDBG_BEFORE"
  fi
  log "restored KFD dynamic-debug state"
}

detect_trace_root() {
  local candidate
  if [[ -n "$TRACE_ROOT" ]] && run_root test -d "$TRACE_ROOT"; then
    return 0
  fi
  for candidate in /sys/kernel/tracing /sys/kernel/debug/tracing; do
    if run_root test -d "$candidate/events"; then
      TRACE_ROOT="$candidate"
      return 0
    fi
  done
  TRACE_ROOT=""
  return 1
}

start_trace() {
  local event enabled=0 enable_file

  (( ENABLE_TRACE == 1 )) || return 0
  if ! detect_trace_root; then
    warn "tracefs is unavailable; skipping scheduler/fence trace"
    return 0
  fi

  TRACE_INSTANCE="${TRACE_ROOT}/instances/kfd_dump_${RUN_TS}_$$"
  if ! run_root mkdir "$TRACE_INSTANCE"; then
    warn "failed to create trace instance: ${TRACE_INSTANCE}"
    TRACE_INSTANCE=""
    return 0
  fi

  mkdir -p "${RUN_DIR}/trace"
  : >"${RUN_DIR}/trace/enabled_events.txt"
  write_root "${TRACE_INSTANCE}/tracing_on" 0
  write_root "${TRACE_INSTANCE}/trace" ""

  for event in "${TRACE_EVENTS[@]}"; do
    enable_file="${TRACE_INSTANCE}/events/${event}/enable"
    if run_root test -e "$enable_file"; then
      if write_root "$enable_file" 1; then
        echo "$event" >>"${RUN_DIR}/trace/enabled_events.txt"
        enabled=$((enabled + 1))
      fi
    else
      warn "trace event is unavailable: ${event}"
    fi
  done

  if (( enabled == 0 )); then
    warn "no requested trace events were enabled"
    run_root rmdir "$TRACE_INSTANCE" 2>/dev/null || true
    TRACE_INSTANCE=""
    return 0
  fi

  write_root "${TRACE_INSTANCE}/tracing_on" 1
  TRACE_ACTIVE=1
  log "started isolated trace instance with ${enabled} events"
}

finish_trace() {
  local destination="${RUN_DIR}/trace/drm_sched_dma_fence.txt"

  [[ -n "$TRACE_INSTANCE" ]] || return 0
  if (( TRACE_ACTIVE == 1 )); then
    write_root "${TRACE_INSTANCE}/tracing_on" 0 2>/dev/null || true
    TRACE_ACTIVE=0
  fi

  if run_root test -e "${TRACE_INSTANCE}/trace"; then
    capture_command "$destination" "scheduler/fence trace" \
      "${ROOT_CMD[@]}" cat "${TRACE_INSTANCE}/trace"
  fi
  run_root rmdir "$TRACE_INSTANCE" 2>/dev/null || true
  TRACE_INSTANCE=""
  log "stopped isolated scheduler/fence trace"
}

restore_output_owner() {
  if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
    chown -R "${SUDO_UID}:${SUDO_GID}" "$RUN_DIR" 2>/dev/null || true
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  finish_trace || true
  restore_dynamic_debug || true
  restore_output_owner
  exit "$rc"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Mount debugfs only when it is actually absent.
if ! run_root test -d "$KFD_DIR"; then
  log "KFD debugfs is missing; attempting to mount debugfs"
  run_root mount -t debugfs none /sys/kernel/debug 2>/dev/null || true
fi
if ! run_root test -d "$KFD_DIR"; then
  record_error "KFD debugfs path is unavailable: ${KFD_DIR}"
  exit 3
fi

log "run_dir=${RUN_DIR}"
log "snapshots=${SNAPSHOTS} interval=${INTERVAL_SEC}s timeout=${READ_TIMEOUT_SEC}s"
if [[ -n "$AMD_SMI" ]]; then
  log "amd_smi=${AMD_SMI}"
fi

enable_dynamic_debug
start_trace

for ((idx = 1; idx <= SNAPSHOTS; idx++)); do
  SNAP_TS="$(date +%Y%m%d_%H%M%S)"
  SNAP_DIR="${RUN_DIR}/snapshot_${idx}_${SNAP_TS}"
  mkdir -p "$SNAP_DIR"
  SNAPSHOT_DIRS+=("$SNAP_DIR")

  log "snapshot ${idx}/${SNAPSHOTS} -> ${SNAP_DIR}"
  {
    echo "started=$(now)"
    echo "snapshot=${idx}/${SNAPSHOTS}"
    echo "hostname=$(hostname)"
    echo "kernel=$(uname -r)"
  } >"${SNAP_DIR}/metadata.txt"

  for name in "${KFD_FILES[@]}"; do
    capture_root_file "${KFD_DIR}/${name}" "${SNAP_DIR}/${name}.txt" "KFD ${name}"
  done

  if [[ -n "$AMD_SMI" && -x "$AMD_SMI" ]]; then
    # Intentionally run bare amd-smi with no subcommands or flags.
    capture_command "${SNAP_DIR}/amd-smi.txt" "amd-smi" "$AMD_SMI"
  else
    warn "amd-smi is unavailable"
  fi

  capture_fences "${SNAP_DIR}/fence_info.txt"
  capture_dmesg "${SNAP_DIR}/dmesg.txt"
  echo "completed=$(now)" >>"${SNAP_DIR}/metadata.txt"

  if (( idx < SNAPSHOTS )); then
    log "sleeping ${INTERVAL_SEC}s before next snapshot"
    sleep "$INTERVAL_SEC"
  fi
done

finish_trace
restore_dynamic_debug

# Compare fence state from the first and last snapshots. With one snapshot,
# take one additional bounded fence sample after the configured interval.
if (( ENABLE_FENCE == 1 )) && (( ${#SNAPSHOT_DIRS[@]} > 0 )); then
  FIRST_FENCE="${SNAPSHOT_DIRS[0]}/fence_info.txt"
  if (( ${#SNAPSHOT_DIRS[@]} == 1 )); then
    log "one snapshot requested; waiting ${INTERVAL_SEC}s for fence comparison"
    sleep "$INTERVAL_SEC"
    LAST_FENCE="${RUN_DIR}/fence_info_after_${INTERVAL_SEC}s.txt"
    capture_fences "$LAST_FENCE"
  else
    LAST_INDEX=$((${#SNAPSHOT_DIRS[@]} - 1))
    LAST_FENCE="${SNAPSHOT_DIRS[$LAST_INDEX]}/fence_info.txt"
  fi

  if [[ -f "$FIRST_FENCE" && -f "$LAST_FENCE" ]]; then
    DIFF_RC=0
    diff -u "$FIRST_FENCE" "$LAST_FENCE" >"${RUN_DIR}/fence_diff.full.txt" || DIFF_RC=$?
    if (( DIFF_RC > 1 )); then
      record_error "fence diff failed (rc=${DIFF_RC})"
    fi
    grep -E '^[+-].*(signaled|emitted|ring)' "${RUN_DIR}/fence_diff.full.txt" \
      >"${RUN_DIR}/fence_diff.filtered.txt" || true
  fi
fi

capture_dmesg "${RUN_DIR}/dmesg-final.txt"

{
  echo "run_dir=${RUN_DIR}"
  echo "started=${RUN_TS}"
  echo "completed=$(now)"
  echo "snapshots=${SNAPSHOTS}"
  echo "interval_sec=${INTERVAL_SEC}"
  echo "timeout_sec=${READ_TIMEOUT_SEC}"
  echo "successful_captures=${SUCCESSFUL_CAPTURES}"
  echo "warnings=${WARNINGS}"
  echo "failures=${FAILURES}"
  echo
  echo -e "artifact\tbytes"
  find "$RUN_DIR" -type f ! -name manifest.txt -printf '%P\t%s\n' | sort
} >"${RUN_DIR}/manifest.txt"

restore_output_owner
log "done: output=${RUN_DIR} captures=${SUCCESSFUL_CAPTURES} warnings=${WARNINGS} failures=${FAILURES}"

if (( FAILURES > 0 )); then
  exit 1
fi
