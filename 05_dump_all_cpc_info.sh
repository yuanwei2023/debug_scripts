#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added multi-GPU CPC, wave, firmware, and kernel diagnostics.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_ARGS=("$@")
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
UMR_OVERRIDE="${UMR:-}"
OUTPUT_OVERRIDE="${OUTPUT_DIR:-}"
NO_SUDO=0
SKIP_HALT_CHECK=0

usage() {
  cat <<'EOF'
Usage: ./05_dump_all_cpc_info.sh [options]

Capture CPC state, waves, KFD runlist, firmware information, AMDGPU module
parameters, and kernel logs for every GPU/XCC, then create a .tgz archive.

Options:
  --umr PATH          UMR executable or source directory
  -o, --output DIR    Output directory
  --no-sudo           Do not relaunch the script with sudo
  --skip-halt-check   Skip enabling and validating halt_if_hws_hang
  -h, --help          Show this help

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
    --no-sudo)
      NO_SUDO=1
      shift
      ;;
    --skip-halt-check)
      SKIP_HALT_CHECK=1
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

if ((EUID != 0 && NO_SUDO == 0)); then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required; run as root or pass --no-sudo" >&2
    exit 1
  fi
  exec sudo env \
    DEBUG_TOOLS_TMP_DIR="$DEFAULT_LOG_ROOT" \
    UMR="$UMR_OVERRIDE" \
    OUTPUT_DIR="$OUTPUT_OVERRIDE" \
    "$SCRIPT_DIR/05_dump_all_cpc_info.sh" "${ORIGINAL_ARGS[@]}"
fi

HALT_PARAMETER="/sys/module/amdgpu/parameters/halt_if_hws_hang"
if ((SKIP_HALT_CHECK == 0)); then
  if [[ ! -r "$HALT_PARAMETER" || ! -w "$HALT_PARAMETER" ]]; then
    echo "Cannot read or write ${HALT_PARAMETER}" >&2
    exit 1
  fi
  halt_value="$(<"$HALT_PARAMETER")"
  if [[ "$halt_value" != "1" && "$halt_value" != "Y" && "$halt_value" != "y" ]]; then
    echo "Enabling halt_if_hws_hang for valid CPC logs"
    if ! printf '1\n' >"$HALT_PARAMETER"; then
      echo "Failed to enable halt_if_hws_hang" >&2
      exit 1
    fi
    halt_value="$(<"$HALT_PARAMETER")"
    if [[ "$halt_value" != "1" && "$halt_value" != "Y" &&
          "$halt_value" != "y" ]]; then
      echo "halt_if_hws_hang is still disabled after writing 1" >&2
      exit 1
    fi
  fi
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
OUTPUT_DIR="${OUTPUT_OVERRIDE:-${DEFAULT_LOG_ROOT}/cpc_dump_${capture_name}}"
ARCHIVE_PATH="${OUTPUT_DIR%/}.tgz"

if ! mkdir -p "$OUTPUT_DIR"; then
  echo "Unable to create output directory: ${OUTPUT_DIR}" >&2
  exit 1
fi

ERROR_LOG="${OUTPUT_DIR}/${capture_name}_errors.log"
COMMAND_LOG="${OUTPUT_DIR}/${capture_name}_commands.log"
SUMMARY_LOG="${OUTPUT_DIR}/${capture_name}_summary.txt"
touch "$ERROR_LOG" "$COMMAND_LOG"

failures=0
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
  echo "Generating ${output_path}"
  if ! "$@" >"$output_path" 2>>"$ERROR_LOG"; then
    echo "Command failed for ${output_path}" | tee -a "$ERROR_LOG" >&2
    failures=$((failures + 1))
    return 1
  fi
}

echo "UMR binary: ${UMR_BIN}"
echo "Output directory: ${OUTPUT_DIR}"

KFD_RLS="/sys/kernel/debug/kfd/rls"
if [[ -r "$KFD_RLS" ]]; then
  capture_command "${OUTPUT_DIR}/${capture_name}_kfd_rls.txt" cat "$KFD_RLS" || true
else
  echo "KFD runlist is not readable: ${KFD_RLS}" | tee -a "$ERROR_LOG" >&2
  failures=$((failures + 1))
fi

log_command "<gpu-list>" "$UMR_BIN" --script instances
if ! gpu_list="$("$UMR_BIN" --script instances 2>>"$ERROR_LOG")"; then
  echo "Failed to enumerate GPU instances" | tee -a "$ERROR_LOG" >&2
  exit 1
fi
if [[ -z "$gpu_list" ]]; then
  echo "UMR did not report any GPU instances" | tee -a "$ERROR_LOG" >&2
  exit 1
fi

for gpu_instance in $gpu_list; do
  log_command "<xcc-list:${gpu_instance}>" \
    "$UMR_BIN" --script xcds "$gpu_instance"
  if ! xcc_list="$("$UMR_BIN" --script xcds "$gpu_instance" 2>>"$ERROR_LOG")"; then
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

    capture_command \
      "${OUTPUT_DIR}/${capture_name}_umr_cpc_${suffix}.txt" \
      "$UMR_BIN" -i "$gpu_instance" -vmp "$xcc" \
      -cpc -O no_backtrace || true

    capture_command \
      "${OUTPUT_DIR}/${capture_name}_umr_waves_${suffix}.txt" \
      "$UMR_BIN" -i "$gpu_instance" -vmp "$xcc" \
      -O bits,halt_waves,no_backtrace -wa none || true

    xcc_count=$((xcc_count + 1))
  done
  gpu_count=$((gpu_count + 1))
done

shopt -s nullglob
firmware_files=(/sys/kernel/debug/dri/*/amdgpu_firmware_info)
if ((${#firmware_files[@]} > 0)); then
  capture_command \
    "${OUTPUT_DIR}/${capture_name}_firmware_info.txt" \
    cat "${firmware_files[0]}" || true
else
  echo "No amdgpu_firmware_info file was found" | tee -a "$ERROR_LOG" >&2
  failures=$((failures + 1))
fi

PARAMS_LOG="${OUTPUT_DIR}/${capture_name}_amdgpu_params.txt"
echo "Generating ${PARAMS_LOG}"
: >"$PARAMS_LOG"
for parameter in /sys/module/amdgpu/parameters/*; do
  if [[ -r "$parameter" ]]; then
    printf '%s = %s\n' \
      "$(basename "$parameter")" "$(<"$parameter")" >>"$PARAMS_LOG"
  fi
done

KERNEL_LOG="${OUTPUT_DIR}/${capture_name}_kernel.log"
if command -v journalctl >/dev/null 2>&1; then
  capture_command "$KERNEL_LOG" journalctl -b -k || true
else
  capture_command "$KERNEL_LOG" dmesg -T || true
fi

{
  echo "umr=${UMR_BIN}"
  echo "gpu_count=${gpu_count}"
  echo "gpu_xcc_count=${xcc_count}"
  echo "failures=${failures}"
  echo "output_dir=${OUTPUT_DIR}"
  echo "archive=${ARCHIVE_PATH}"
} >"$SUMMARY_LOG"

echo "Creating archive: ${ARCHIVE_PATH}"
if ! tar -czf "$ARCHIVE_PATH" \
  -C "$(dirname "$OUTPUT_DIR")" "$(basename "$OUTPUT_DIR")"; then
  echo "Failed to create archive: ${ARCHIVE_PATH}" >&2
  exit 1
fi

echo "CPC dump complete: ${ARCHIVE_PATH}"
if ((failures > 0)); then
  echo "Capture completed with ${failures} failure(s); see ${ERROR_LOG}" >&2
  exit 2
fi
