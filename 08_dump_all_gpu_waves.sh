#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added standalone wave capture for every GPU and XCC.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_ARGS=("$@")
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
UMR_OVERRIDE="${UMR:-}"
OUTPUT_OVERRIDE="${OUTPUT_DIR:-}"
SAMPLES=5
INTERVAL=5
READ_TIMEOUT=120
HALT_WAVES=0
NO_SUDO=0
CREATE_ARCHIVE=1

usage() {
  cat <<'EOF'
Usage: ./08_dump_all_gpu_waves.sh [options]

Capture active wave state from every UMR GPU and every XCC.

Options:
  --umr PATH           UMR executable or source directory
  -o, --output DIR     Output directory
  --samples N          Number of wave snapshots (default: 5)
  --interval SEC       Seconds between snapshots (default: 5)
  --timeout SEC        Timeout for each UMR command (default: 120)
  --halt-waves         Temporarily halt waves for a coherent register snapshot
  --no-sudo            Do not relaunch the script with sudo
  --no-archive         Do not create the final .tgz archive
  -h, --help           Show this help

Notes:
  --halt-waves is intrusive and should only be used on a debug system.
  Without it, waves can change while UMR scans the GPU.

Environment overrides:
  UMR, OUTPUT_DIR, DEBUG_TOOLS_TMP_DIR
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
    --umr)
      require_value "$@"
      UMR_OVERRIDE="$2"
      shift 2
      ;;
    -o|--output)
      require_value "$@"
      OUTPUT_OVERRIDE="$2"
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
    --timeout)
      require_value "$@"
      READ_TIMEOUT="$2"
      shift 2
      ;;
    --halt-waves)
      HALT_WAVES=1
      shift
      ;;
    --no-sudo)
      NO_SUDO=1
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

if [[ ! "$SAMPLES" =~ ^[1-9][0-9]*$ ]]; then
  echo "Samples must be a positive integer: ${SAMPLES}" >&2
  exit 2
fi
if [[ ! "$INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "Interval must be a non-negative number: ${INTERVAL}" >&2
  exit 2
fi
if [[ ! "$READ_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "Timeout must be a positive integer: ${READ_TIMEOUT}" >&2
  exit 2
fi
if ! command -v timeout >/dev/null 2>&1; then
  echo "Missing required command: timeout" >&2
  exit 1
fi

if ((EUID != 0 && NO_SUDO == 0)); then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required; run as root or pass --no-sudo" >&2
    exit 1
  fi
  exec sudo env \
    DEBUG_TOOLS_TMP_DIR="$DEFAULT_LOG_ROOT" \
    UMR="$UMR_OVERRIDE" \
    OUTPUT_DIR="$OUTPUT_OVERRIDE" \
    "$SCRIPT_DIR/08_dump_all_gpu_waves.sh" "${ORIGINAL_ARGS[@]}"
fi

resolve_umr() {
  local candidate
  local explicit="$UMR_OVERRIDE"

  if [[ -n "$explicit" ]]; then
    if [[ -x "$explicit" && ! -d "$explicit" ]]; then
      printf '%s\n' "$explicit"
      return 0
    fi
    if [[ -d "$explicit" ]]; then
      for candidate in \
        "$explicit/build/src/app/umr" \
        "$explicit/src/app/umr"; do
        if [[ -x "$candidate" ]]; then
          printf '%s\n' "$candidate"
          return 0
        fi
      done
    fi
    echo "UMR was not found under: ${explicit}" >&2
    return 1
  fi

  for candidate in \
    /home/weiyuan2/umr/build/src/app/umr \
    /home/weiyuan2/test-case/ali-models/umr/build/src/app/umr \
    /home/admin/umr/src/app/umr \
    /home/admin/umr/build/src/app/umr \
    /usr/local/bin/umr; do
    if [[ -x "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  if command -v umr >/dev/null 2>&1; then
    command -v umr
    return 0
  fi
  echo "Unable to find UMR; pass --umr PATH or set UMR" >&2
  return 1
}

if ! UMR_BIN="$(resolve_umr)"; then
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
hostname_short="$(hostname -s 2>/dev/null || hostname)"
capture_name="${hostname_short}_${timestamp}"
OUTPUT_DIR="${OUTPUT_OVERRIDE:-${DEFAULT_LOG_ROOT}/wave_dump_${capture_name}}"

if [[ -e "$OUTPUT_DIR" ]]; then
  echo "Output path already exists: ${OUTPUT_DIR}" >&2
  exit 1
fi
if ! mkdir -p "$OUTPUT_DIR"; then
  echo "Unable to create output directory: ${OUTPUT_DIR}" >&2
  exit 1
fi
OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd)"
ARCHIVE_PATH="${OUTPUT_DIR%/}.tgz"

ERROR_LOG="${OUTPUT_DIR}/${capture_name}_errors.txt"
COMMAND_LOG="${OUTPUT_DIR}/${capture_name}_commands.txt"
SUMMARY_LOG="${OUTPUT_DIR}/${capture_name}_summary.txt"
: >"$ERROR_LOG"
: >"$COMMAND_LOG"

failures=0
captures=0
gpu_count=0
xcc_count=0

log_command() {
  local output_path="$1"
  shift
  {
    printf 'output=%q command=' "$output_path"
    printf '%q ' "$@"
    printf '\n'
  } >>"$COMMAND_LOG"
}

capture_command() {
  local output_path="$1"
  shift
  log_command "$output_path" "$@"
  echo "[wave-dump] BEGIN ${output_path}"
  if timeout "$READ_TIMEOUT" "$@" >"$output_path" 2>>"$ERROR_LOG"; then
    captures=$((captures + 1))
    echo "[wave-dump] END ${output_path}: PASS"
  else
    local rc=$?
    failures=$((failures + 1))
    echo "[wave-dump] END ${output_path}: FAIL rc=${rc}" |
      tee -a "$ERROR_LOG" >&2
  fi
}

log_command "<gpu-list>" "$UMR_BIN" --script instances
if ! gpu_list="$(
  timeout "$READ_TIMEOUT" "$UMR_BIN" --script instances 2>>"$ERROR_LOG"
)"; then
  echo "Failed to enumerate GPU instances" | tee -a "$ERROR_LOG" >&2
  exit 1
fi
if [[ -z "$gpu_list" ]]; then
  echo "UMR did not report any GPU instances" | tee -a "$ERROR_LOG" >&2
  exit 1
fi

wave_options="bits,no_backtrace"
if ((HALT_WAVES == 1)); then
  wave_options+=",halt_waves"
fi

for ((sample = 0; sample < SAMPLES; sample++)); do
  sample_dir="${OUTPUT_DIR}/sample-$(printf '%02d' "$sample")"
  mkdir -p "$sample_dir"
  gpu_count=0

  for gpu_instance in $gpu_list; do
    log_command "<xcc-list:${gpu_instance}>" \
      "$UMR_BIN" --script xcds "$gpu_instance"
    if ! xcc_list="$(
      timeout "$READ_TIMEOUT" \
        "$UMR_BIN" --script xcds "$gpu_instance" 2>>"$ERROR_LOG"
    )"; then
      xcc_list=""
    fi
    if [[ -z "$xcc_list" ]]; then
      xcc_list="-1"
    fi

    for xcc in $xcc_list; do
      suffix="gpu${gpu_count}"
      if [[ "$xcc" != "-1" ]]; then
        suffix+="_xcc${xcc}"
      fi
      output_path="${sample_dir}/${capture_name}_waves_${suffix}.txt"
      capture_command \
        "$output_path" \
        "$UMR_BIN" -i "$gpu_instance" -vmp "$xcc" \
        -O "$wave_options" -wa none
      xcc_count=$((xcc_count + 1))
    done
    gpu_count=$((gpu_count + 1))
  done

  if ((sample + 1 < SAMPLES)); then
    echo "[wave-dump] Sleeping ${INTERVAL}s before next snapshot"
    sleep "$INTERVAL"
  fi
done

{
  echo "umr=${UMR_BIN}"
  echo "gpu_count=${gpu_count}"
  echo "gpu_xcc_captures=${xcc_count}"
  echo "samples=${SAMPLES}"
  echo "interval_sec=${INTERVAL}"
  echo "timeout_sec=${READ_TIMEOUT}"
  echo "halt_waves=${HALT_WAVES}"
  echo "successful_captures=${captures}"
  echo "failures=${failures}"
  echo "output_dir=${OUTPUT_DIR}"
  echo "archive=${ARCHIVE_PATH}"
  echo "completed_at=$(date --iso-8601=seconds)"
} >"$SUMMARY_LOG"

if ((CREATE_ARCHIVE == 1)); then
  echo "[wave-dump] Creating archive: ${ARCHIVE_PATH}"
  if ! tar -czf "$ARCHIVE_PATH" \
    -C "$(dirname "$OUTPUT_DIR")" "$(basename "$OUTPUT_DIR")"; then
    echo "Failed to create archive: ${ARCHIVE_PATH}" >&2
    exit 1
  fi
else
  echo "[wave-dump] Archive creation disabled"
fi

echo "[wave-dump] Complete: ${OUTPUT_DIR}"
if ((failures > 0)); then
  echo "Capture completed with ${failures} failure(s); see ${ERROR_LOG}" >&2
  exit 2
fi
