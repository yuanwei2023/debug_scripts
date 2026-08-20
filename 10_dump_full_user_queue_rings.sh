#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-05 - Added full retained user-queue ring capture by PID.

set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ORIGINAL_ARGS=("$@")
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
UMR_OVERRIDE="${UMR:-}"
OUTPUT_OVERRIDE="${OUTPUT_DIR:-}"
PID=""
VM_PARTITION=0
READ_TIMEOUT=120
NO_SUDO=0
CREATE_ARCHIVE=1
QUEUE_FILTERS=()
PCI_FILTERS=()

usage() {
  cat <<'EOF'
Usage: ./10_dump_full_user_queue_rings.sh --pid PID [options]

Dump the full retained UMR user-queue ring for every KFD queue owned by PID.
Each queue is mapped to its GPU through KFD queue gpuid and topology metadata.

Required:
  --pid PID                 Target KFD process ID

Options:
  --queue ID                Only dump this queue ID; may be repeated
  --pci BDF                 Only dump queues on this PCI BDF; may be repeated
  --vm-partition N          UMR VM partition (default: 0)
  --umr PATH                UMR executable or source directory
  -o, --output DIR          Output directory
  --timeout SEC             Timeout for each UMR command (default: 120)
  --no-sudo                 Do not relaunch with sudo
  --no-archive              Do not create the final .tgz archive
  -h, --help                Show this help

Examples:
  ./10_dump_full_user_queue_rings.sh --pid 875176
  ./10_dump_full_user_queue_rings.sh --pid 875176 --queue 28
  ./10_dump_full_user_queue_rings.sh --pid 875176 \
    --pci 0000:0a:00.0 --vm-partition 0

Notes:
  Full rings can be large and contain stale packets that have already been
  consumed. The current RPTR/WPTR in each dump must be used to distinguish
  live packets from retained ring history.

Environment:
  UMR, OUTPUT_DIR, DEBUG_TOOLS_TMP_DIR
EOF
}

require_value() {
  if (($# < 2)) || [[ -z "$2" ]]; then
    echo "Option $1 requires a value" >&2
    exit 2
  fi
}

normalize_bdf() {
  local value="${1,,}"
  if [[ "$value" =~ ^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]]; then
    value="0000:${value}"
  fi
  if [[ ! "$value" =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]]; then
    return 1
  fi
  printf '%s\n' "$value"
}

while (($# > 0)); do
  case "$1" in
    --pid)
      require_value "$@"
      PID="$2"
      shift 2
      ;;
    --queue)
      require_value "$@"
      QUEUE_FILTERS+=("$2")
      shift 2
      ;;
    --pci)
      require_value "$@"
      if ! normalized_bdf="$(normalize_bdf "$2")"; then
        echo "Invalid PCI BDF: $2" >&2
        exit 2
      fi
      PCI_FILTERS+=("$normalized_bdf")
      shift 2
      ;;
    --vm-partition)
      require_value "$@"
      VM_PARTITION="$2"
      shift 2
      ;;
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
    --timeout)
      require_value "$@"
      READ_TIMEOUT="$2"
      shift 2
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

if [[ ! "$PID" =~ ^[1-9][0-9]*$ ]]; then
  echo "--pid must be a positive integer" >&2
  exit 2
fi
if [[ ! "$VM_PARTITION" =~ ^[0-9]+$ ]]; then
  echo "--vm-partition must be a non-negative integer: ${VM_PARTITION}" >&2
  exit 2
fi
if [[ ! "$READ_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
  echo "--timeout must be a positive integer: ${READ_TIMEOUT}" >&2
  exit 2
fi
for queue_id in "${QUEUE_FILTERS[@]}"; do
  if [[ ! "$queue_id" =~ ^[0-9]+$ ]]; then
    echo "--queue must be a non-negative integer: ${queue_id}" >&2
    exit 2
  fi
done
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
    "$SCRIPT_DIR/10_dump_full_user_queue_rings.sh" \
    --no-sudo "${ORIGINAL_ARGS[@]}"
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

QUEUE_ROOT="/sys/class/kfd/kfd/proc/${PID}/queues"
TOPOLOGY_ROOT="/sys/class/kfd/kfd/topology/nodes"
if [[ ! -d "$QUEUE_ROOT" ]]; then
  echo "KFD queue metadata is unavailable for PID ${PID}: ${QUEUE_ROOT}" >&2
  echo "Confirm that the process is still running and KFD sysfs is mounted." >&2
  exit 1
fi
if [[ ! -d "$TOPOLOGY_ROOT" ]]; then
  echo "KFD topology metadata is unavailable: ${TOPOLOGY_ROOT}" >&2
  exit 1
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
hostname_short="$(hostname -s 2>/dev/null || hostname)"
capture_name="full_uq_ring_pid${PID}_${hostname_short}_${timestamp}"
OUTPUT_DIR="${OUTPUT_OVERRIDE:-${DEFAULT_LOG_ROOT}/${capture_name}}"
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
COMMAND_LOG="${OUTPUT_DIR}/${capture_name}_commands.txt"
ERROR_LOG="${OUTPUT_DIR}/${capture_name}_errors.txt"
SUMMARY_LOG="${OUTPUT_DIR}/${capture_name}_summary.txt"
QUEUE_MAP="${OUTPUT_DIR}/${capture_name}_queue_map.tsv"
: >"$COMMAND_LOG"
: >"$ERROR_LOG"
printf 'queue_id\tkfd_gpu_id\tpci_bdf\tstatus\toutput\n' >"$QUEUE_MAP"

declare -A BDF_BY_GPU_ID=()
declare -A LISTED_BDFS=()
declare -A FOUND_QUEUE_IDS=()

read_topology_property() {
  local properties_path="$1"
  local property_name="$2"
  awk -v key="$property_name" '$1 == key { print $2; exit }' "$properties_path"
}

for node_path in "$TOPOLOGY_ROOT"/[0-9]*; do
  [[ -d "$node_path" ]] || continue
  gpu_id_path="${node_path}/gpu_id"
  properties_path="${node_path}/properties"
  [[ -r "$gpu_id_path" && -r "$properties_path" ]] || continue
  gpu_id="$(<"$gpu_id_path")"
  [[ "$gpu_id" =~ ^[1-9][0-9]*$ ]] || continue
  location_id="$(read_topology_property "$properties_path" location_id)"
  domain="$(read_topology_property "$properties_path" domain)"
  [[ "$location_id" =~ ^[0-9]+$ ]] || continue
  [[ "$domain" =~ ^[0-9]+$ ]] || domain=0
  bus=$(((location_id >> 8) & 0xff))
  device=$(((location_id >> 3) & 0x1f))
  function=$((location_id & 0x7))
  printf -v pci_bdf '%04x:%02x:%02x.%x' \
    "$domain" "$bus" "$device" "$function"
  BDF_BY_GPU_ID["$gpu_id"]="$pci_bdf"
done

queue_requested() {
  local candidate="$1"
  local requested
  if ((${#QUEUE_FILTERS[@]} == 0)); then
    return 0
  fi
  for requested in "${QUEUE_FILTERS[@]}"; do
    [[ "$candidate" == "$requested" ]] && return 0
  done
  return 1
}

pci_requested() {
  local candidate="$1"
  local requested
  if ((${#PCI_FILTERS[@]} == 0)); then
    return 0
  fi
  for requested in "${PCI_FILTERS[@]}"; do
    [[ "$candidate" == "$requested" ]] && return 0
  done
  return 1
}

log_command() {
  local output_path="$1"
  shift
  {
    printf 'output=%q command=' "$output_path"
    printf '%q ' "$@"
    printf '\n'
  } >>"$COMMAND_LOG"
}

capture_list_uq() {
  local pci_bdf="$1"
  local safe_bdf="${pci_bdf//[:.]/_}"
  local output_path="${OUTPUT_DIR}/gpu_${safe_bdf}_list_uq.txt"
  local command=(
    "$UMR_BIN"
    --by-pci "$pci_bdf"
    --vm-partition "$VM_PARTITION"
    --list-uq
  )
  local rc=0

  [[ -z "${LISTED_BDFS[$pci_bdf]:-}" ]] || return 0
  LISTED_BDFS["$pci_bdf"]=1
  log_command "$output_path" "${command[@]}"
  {
    printf '$'
    printf ' %q' "${command[@]}"
    printf '\n'
  } >"$output_path"
  timeout "$READ_TIMEOUT" "${command[@]}" >>"$output_path" 2>&1 || rc=$?
  printf '\n[exit=%d]\n' "$rc" >>"$output_path"
  if ((rc != 0)); then
    echo "[full-uq] Queue listing failed for ${pci_bdf}: rc=${rc}" |
      tee -a "$ERROR_LOG" >&2
  fi
}

captures=0
failures=0
skipped=0
discovered=0
selected=0

for queue_path in "$QUEUE_ROOT"/[0-9]*; do
  [[ -d "$queue_path" ]] || continue
  queue_id="${queue_path##*/}"
  queue_requested "$queue_id" || continue
  discovered=$((discovered + 1))
  FOUND_QUEUE_IDS["$queue_id"]=1

  gpuid_path="${queue_path}/gpuid"
  if [[ ! -r "$gpuid_path" ]]; then
    echo "[full-uq] Q${queue_id}: missing readable gpuid" |
      tee -a "$ERROR_LOG" >&2
    printf '%s\t-\t-\tfailed\tmissing-gpuid\n' "$queue_id" >>"$QUEUE_MAP"
    failures=$((failures + 1))
    continue
  fi
  kfd_gpu_id="$(<"$gpuid_path")"
  pci_bdf="${BDF_BY_GPU_ID[$kfd_gpu_id]:-}"
  if [[ -z "$pci_bdf" ]]; then
    echo "[full-uq] Q${queue_id}: cannot map KFD GPU ${kfd_gpu_id} to PCI" |
      tee -a "$ERROR_LOG" >&2
    printf '%s\t%s\t-\tfailed\tmissing-topology-map\n' \
      "$queue_id" "$kfd_gpu_id" >>"$QUEUE_MAP"
    failures=$((failures + 1))
    continue
  fi
  if ! pci_requested "$pci_bdf"; then
    skipped=$((skipped + 1))
    continue
  fi
  selected=$((selected + 1))

  capture_list_uq "$pci_bdf"
  safe_bdf="${pci_bdf//[:.]/_}"
  output_path="${OUTPUT_DIR}/gpu_${safe_bdf}_queue_${queue_id}_full_ring.txt"
  command=(
    "$UMR_BIN"
    --by-pci "$pci_bdf"
    --vm-partition "$VM_PARTITION"
    --user-queue "kfd,pid=${PID},queue=${queue_id}"
    -O use_full_user_queue,no_backtrace
    --dump-uq
  )
  log_command "$output_path" "${command[@]}"
  {
    printf '$'
    printf ' %q' "${command[@]}"
    printf '\n'
  } >"$output_path"

  echo "[full-uq] BEGIN Q${queue_id} gpu=${kfd_gpu_id} pci=${pci_bdf}"
  rc=0
  timeout "$READ_TIMEOUT" "${command[@]}" >>"$output_path" 2>&1 || rc=$?
  printf '\n[exit=%d]\n' "$rc" >>"$output_path"
  if ((rc == 0)) && grep -Eq \
    '^Dumping 0x[0-9a-fA-F]+ words from user queue-' "$output_path"; then
    captures=$((captures + 1))
    status="captured"
    echo "[full-uq] END Q${queue_id}: PASS"
  else
    if ((rc == 0)); then
      rc=1
      echo "[full-uq] Q${queue_id}: UMR returned no queue dump" \
        >>"$ERROR_LOG"
    fi
    failures=$((failures + 1))
    status="failed"
    echo "[full-uq] END Q${queue_id}: FAIL rc=${rc}" |
      tee -a "$ERROR_LOG" >&2
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$queue_id" "$kfd_gpu_id" "$pci_bdf" "$status" \
    "$(basename "$output_path")" >>"$QUEUE_MAP"
done

for requested_queue in "${QUEUE_FILTERS[@]}"; do
  if [[ -z "${FOUND_QUEUE_IDS[$requested_queue]:-}" ]]; then
    echo "[full-uq] Requested Q${requested_queue} does not exist for PID ${PID}" |
      tee -a "$ERROR_LOG" >&2
    failures=$((failures + 1))
  fi
done

if ((selected == 0)); then
  echo "No matching KFD queues found for PID ${PID}" |
    tee -a "$ERROR_LOG" >&2
  failures=$((failures + 1))
fi

{
  echo "pid=${PID}"
  echo "umr=${UMR_BIN}"
  echo "vm_partition=${VM_PARTITION}"
  echo "timeout_sec=${READ_TIMEOUT}"
  echo "full_ring_option=use_full_user_queue"
  echo "queue_filter_matches=${discovered}"
  echo "selected_queues=${selected}"
  echo "successful_captures=${captures}"
  echo "skipped_by_pci_filter=${skipped}"
  echo "failures=${failures}"
  echo "output_dir=${OUTPUT_DIR}"
  echo "archive=${ARCHIVE_PATH}"
  echo "completed_at=$(date --iso-8601=seconds)"
} >"$SUMMARY_LOG"

if ((CREATE_ARCHIVE == 1)); then
  echo "[full-uq] Creating archive: ${ARCHIVE_PATH}"
  if ! tar -czf "$ARCHIVE_PATH" \
    -C "$(dirname "$OUTPUT_DIR")" "$(basename "$OUTPUT_DIR")"; then
    echo "Failed to create archive: ${ARCHIVE_PATH}" >&2
    exit 1
  fi
else
  echo "[full-uq] Archive creation disabled"
fi

echo "[full-uq] Complete: ${OUTPUT_DIR}"
if ((failures > 0)); then
  echo "Capture completed with ${failures} failure(s); see ${ERROR_LOG}" >&2
  exit 2
fi
