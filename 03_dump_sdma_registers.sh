#!/usr/bin/env bash
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added read-only multi-GPU SDMA register collection.
#
# Read selected SDMA registers from every UMR GPU/SDMA instance into one log.
# Read-only: this script uses UMR -lb and -r; it never writes registers.
# The log uses the KFD/ROCm Node ID as gpu= and maps it to bare amd-smi output.

set -u
set -o pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
UMR="${UMR:-}"
AMD_SMI="${AMD_SMI:-$(command -v amd-smi 2>/dev/null || true)}"
DMESG="${DMESG:-$(command -v dmesg 2>/dev/null || true)}"
VMP="${VMP:-1}"
LOG_FILE="${LOG_FILE:-${DEFAULT_LOG_ROOT}/sdma_registers_$(date +%Y%m%d_%H%M%S).log}"

# Existing control register plus all requested status, queue, RLC, and NACK
# registers. RLC0..RLC7 entries are generated below to avoid copy/paste gaps.
REGISTERS=(
  # Existing SDMA control register
  SDMA_CNTL

  # SDMA Status registers
  SDMA_UCODE_CHECKSUM
  SDMA_STATUS_REG
  SDMA_STATUS1_REG
  SDMA_STATUS2_REG
  SDMA_STATUS3_REG
  SDMA_STATUS4_REG
  # sdma0445 does not expose SDMA_INT_STATUS in the SDMA IP block database.

  # SDMA GFX Queue
  SDMA_GFX_CONTEXT_STATUS
  SDMA_GFX_RB_CNTL
  SDMA_GFX_RB_RPTR
  SDMA_GFX_RB_WPTR
  SDMA_GFX_RB_PREEMPT
  SDMA_GFX_IB_CNTL
  SDMA_GFX_IB_OFFSET
  SDMA_GFX_IB_RPTR
  SDMA_GFX_IB_SIZE

  # SDMA Paging Queue
  SDMA_PAGE_CONTEXT_STATUS
  SDMA_PAGE_RB_CNTL
  SDMA_PAGE_RB_RPTR
  SDMA_PAGE_RB_WPTR
  SDMA_PAGE_RB_PREEMPT
  SDMA_PAGE_IB_CNTL
  SDMA_PAGE_IB_OFFSET
  SDMA_PAGE_IB_RPTR
  SDMA_PAGE_IB_SIZE
)

# SDMA RLC user queues 0..7 (9 registers per queue).
for rlc in {0..7}; do
  REGISTERS+=(
    "SDMA_RLC${rlc}_CONTEXT_STATUS"
    "SDMA_RLC${rlc}_RB_CNTL"
    "SDMA_RLC${rlc}_RB_RPTR"
    "SDMA_RLC${rlc}_RB_WPTR"
    "SDMA_RLC${rlc}_RB_PREEMPT"
    "SDMA_RLC${rlc}_IB_CNTL"
    "SDMA_RLC${rlc}_IB_OFFSET"
    "SDMA_RLC${rlc}_IB_RPTR"
    "SDMA_RLC${rlc}_IB_SIZE"
  )
done

# SDMA NACK registers
REGISTERS+=(
  SDMA_UTCL1_RD_XNACK0
  SDMA_UTCL1_RD_XNACK1
  SDMA_UTCL1_WR_XNACK0
  SDMA_UTCL1_WR_XNACK1
)

usage() {
  cat <<'USAGE'
Usage: 03_dump_sdma_registers.sh [-o LOG_FILE] [--vmp N]

Read selected SDMA registers from all GPUs and discovered SDMA instances.
The log uses the KFD/ROCm Node ID as gpu= and maps it to the GPU number shown
by the normal, no-argument amd-smi command. dmesg is appended at the end.

Options:
  -o, --output FILE  Output log path
                     (default: /tmp/debug-tools-<UID>/sdma_registers_<timestamp>.log)
      --vmp N        UMR VM partition (default: 1)
  -h, --help         Show this help

Environment:
  UMR       UMR executable or source directory (default: auto-detect)
  AMD_SMI   AMD-SMI executable (default: auto-detected amd-smi)
  DMESG     dmesg executable (default: auto-detected dmesg)
  VMP       UMR VM partition (default: 1)
  LOG_FILE  Output log path
  DEBUG_TOOLS_TMP_DIR  Default temporary output root
USAGE
}

while (( $# > 0 )); do
  case "$1" in
    -o|--output)
      if (( $# < 2 )); then
        echo "ERROR: $1 requires a file path" >&2
        exit 1
      fi
      LOG_FILE="$2"
      shift 2
      ;;
    --vmp)
      if (( $# < 2 )); then
        echo "ERROR: --vmp requires a number" >&2
        exit 1
      fi
      VMP="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! "$VMP" =~ ^[0-9]+$ ]]; then
  echo "ERROR: invalid VMP value: $VMP" >&2
  exit 1
fi

if [[ -n "$UMR" && -d "$UMR" ]]; then
  umr_source_dir="${UMR%/}"
  UMR=""
  for candidate in \
    "${umr_source_dir}/build/src/app/umr" \
    "${umr_source_dir}/src/app/umr"; do
    if [[ -x "$candidate" ]]; then
      UMR="$candidate"
      break
    fi
  done
fi

if [[ -z "$UMR" ]]; then
  for candidate in \
    /home/weiyuan2/umr/build/src/app/umr \
    /home/weiyuan2/test-case/ali-models/umr/build/src/app/umr \
    /home/admin/umr/src/app/umr \
    /home/admin/umr/build/src/app/umr \
    /usr/local/bin/umr; do
    if [[ -x "$candidate" ]]; then
      UMR="$candidate"
      break
    fi
  done
fi

if [[ -z "$UMR" ]]; then
  UMR="$(command -v umr 2>/dev/null || true)"
fi

if [[ ! -x "$UMR" ]]; then
  echo "ERROR: UMR executable not found: $UMR" >&2
  exit 1
fi

iso_time() {
  date '+%Y-%m-%dT%H:%M:%S.%3N%:z'
}

normalize_bdf() {
  printf '%s' "${1,,}"
}

run_privileged() {
  if (( EUID == 0 )); then
    "$@"
  else
    sudo "$@"
  fi
}

run_umr() {
  run_privileged "$UMR" "$@"
}

mkdir -p "$(dirname -- "$LOG_FILE")"
: >"$LOG_FILE"
# Keep the interactive progress/error stream while preserving the same output
# in the artifact. Explicit per-command sections below continue to append
# directly to this file.
exec > >(tee -a "$LOG_FILE") 2>&1

TOTAL=0
SUCCESS=0
FAILED=0
BLOCKS=0
DISCOVERY_FAILURES=0
AMD_SMI_RC=127
AMD_SMI_OUTPUT=""

declare -A AMD_INDEX_BY_BDF=()
declare -A NODE_ID_BY_BDF=()
declare -A GPU_ID_BY_UMR=()
declare -A AMD_INDEX_BY_UMR=()
declare -A BDF_BY_UMR=()

# Run AMD-SMI exactly once with no arguments. Its output is both logged and
# parsed for the PCI-BDF-to-AMD-SMI-GPU mapping.
if [[ -n "$AMD_SMI" && -x "$AMD_SMI" ]]; then
  AMD_SMI_RC=0
  AMD_SMI_OUTPUT="$("$AMD_SMI" 2>&1)" || AMD_SMI_RC=$?

  current_bdf=""
  while IFS= read -r line; do
    if [[ "$line" =~ \|[[:space:]]+([0-9A-Fa-f]{4}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7])[[:space:]] ]]; then
      current_bdf="$(normalize_bdf "${BASH_REMATCH[1]}")"
    elif [[ -n "$current_bdf" && "$line" =~ ^\|[[:space:]]+([0-9]+)[[:space:]]+([0-9]+)[[:space:]]+ ]]; then
      AMD_INDEX_BY_BDF["$current_bdf"]="${BASH_REMATCH[1]}"
      current_bdf=""
    fi
  done <<<"$AMD_SMI_OUTPUT"
fi

# Resolve the KFD/ROCm Node ID from sysfs. The KFD location_id encodes
# bus/device/function; PCI BDF is the stable join key shared with AMD-SMI/UMR.
shopt -s nullglob
for node_dir in /sys/class/kfd/kfd/topology/nodes/[0-9]*; do
  [[ -r "$node_dir/properties" ]] || continue
  kfd_gpu_id="$(cat "$node_dir/gpu_id" 2>/dev/null || echo 0)"
  [[ "$kfd_gpu_id" =~ ^[0-9]+$ ]] && (( kfd_gpu_id > 0 )) || continue

  domain="$(awk '$1 == "domain" { print $2; exit }' "$node_dir/properties")"
  location_id="$(awk '$1 == "location_id" { print $2; exit }' "$node_dir/properties")"
  [[ "$domain" =~ ^[0-9]+$ && "$location_id" =~ ^[0-9]+$ ]] || continue

  printf -v bdf '%04x:%02x:%02x.%x' \
    "$domain" \
    "$(( (location_id >> 8) & 0xff ))" \
    "$(( (location_id >> 3) & 0x1f ))" \
    "$(( location_id & 0x7 ))"
  NODE_ID_BY_BDF["$(normalize_bdf "$bdf")"]="${node_dir##*/}"
done
shopt -u nullglob

mapfile -t UMR_INSTANCES < <(
  run_umr --script instances 2>/dev/null |
    tr ',\t' '  ' |
    awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^[0-9]+$/ && !seen[$i]++) print $i }'
)

if (( ${#UMR_INSTANCES[@]} == 0 )); then
  {
    echo "# SDMA register dump"
    echo "# started=$(iso_time)"
    echo "ERROR: no GPU instances returned by 'umr --script instances'"
    echo
    echo "# AMD-SMI snapshot"
    echo "# command: amd-smi"
    echo "# exit_code=$AMD_SMI_RC"
    [[ -n "$AMD_SMI_OUTPUT" ]] && printf '%s\n' "$AMD_SMI_OUTPUT"
  } >>"$LOG_FILE"
  echo "ERROR: no GPU instances returned by 'umr --script instances'" >&2
  exit 1
fi

# Join UMR's DRM card instance to Node ID and AMD-SMI GPU number via PCI BDF.
for umr_instance in "${UMR_INSTANCES[@]}"; do
  device_link="/sys/class/drm/card${umr_instance}/device"
  bdf="unknown"
  if [[ -e "$device_link" ]]; then
    bdf="$(normalize_bdf "$(basename "$(readlink -f "$device_link")")")"
  fi

  GPU_ID_BY_UMR["$umr_instance"]="${NODE_ID_BY_BDF[$bdf]:-unknown}"
  AMD_INDEX_BY_UMR["$umr_instance"]="${AMD_INDEX_BY_BDF[$bdf]:-unknown}"
  BDF_BY_UMR["$umr_instance"]="$bdf"
done

# Process and print GPUs in ascending KFD/ROCm Node ID order.
mapfile -t SORTED_UMR_INSTANCES < <(
  for umr_instance in "${UMR_INSTANCES[@]}"; do
    gpu_id="${GPU_ID_BY_UMR[$umr_instance]}"
    if [[ "$gpu_id" =~ ^[0-9]+$ ]]; then
      printf '%010d %s\n' "$gpu_id" "$umr_instance"
    else
      printf '9999999999 %s\n' "$umr_instance"
    fi
  done | sort -n -k1,1 | awk '{print $2}'
)

# Write all identification information and bare AMD-SMI output immediately, so
# they remain available even if register collection is interrupted later.
{
  echo "# SDMA register dump"
  echo "# started=$(iso_time)"
  echo "# hostname=$(hostname)"
  echo "# UMR=$UMR"
  echo "# gpu_index_policy=KFD/ROCm Node ID"
  echo "# VMP=$VMP"
  echo "# requested_register_count=${#REGISTERS[@]}"
  echo "# requested_registers=${REGISTERS[*]}"
  echo "# UMR register aliases: GFX_RB_PREEMPT=GFX_PREEMPT PAGE_RB_PREEMPT=PAGE_PREEMPT RLC<n>_RB_PREEMPT=RLC<n>_PREEMPT"
  echo
  echo "# GPU mapping"
  echo "# gpu is KFD/ROCm Node ID; amd_smi_gpu is the GPU number shown by amd-smi."
  echo "# columns: gpu amd_smi_gpu pci_bdf"
  for umr_instance in "${SORTED_UMR_INSTANCES[@]}"; do
    printf '# gpu=%s amd_smi_gpu=%s pci_bdf=%s\n' \
      "${GPU_ID_BY_UMR[$umr_instance]}" \
      "${AMD_INDEX_BY_UMR[$umr_instance]}" \
      "${BDF_BY_UMR[$umr_instance]}"
  done
  echo
  echo "# AMD-SMI snapshot"
  echo "# command: amd-smi"
  echo "# exit_code=$AMD_SMI_RC"
  if [[ -n "$AMD_SMI_OUTPUT" ]]; then
    printf '%s\n' "$AMD_SMI_OUTPUT"
  else
    echo "[ERROR] amd-smi unavailable or produced no output"
  fi
  echo
} >>"$LOG_FILE"

echo "Reading ${#REGISTERS[@]} SDMA registers; log: $LOG_FILE"

for umr_instance in "${SORTED_UMR_INSTANCES[@]}"; do
  gpu_id="${GPU_ID_BY_UMR[$umr_instance]}"
  bdf="${BDF_BY_UMR[$umr_instance]}"

  block_list_rc=0
  block_list_output="$(run_umr -i "$umr_instance" -lb 2>&1)" || block_list_rc=$?
  {
    printf '=== gpu=%s amd_smi_gpu=%s pci_bdf=%s list-blocks ===\n' \
      "$gpu_id" "${AMD_INDEX_BY_UMR[$umr_instance]}" "$bdf"
    printf '# command: %s -i %s --list-blocks\n' "$UMR" "$umr_instance"
    printf '# exit_code=%d\n' "$block_list_rc"
    printf '%s\n\n' "$block_list_output"
  } >>"$LOG_FILE"

  mapfile -t sdma_blocks < <(
    printf '%s\n' "$block_list_output" |
      awk '$1 ~ /\.sdma[0-9]+\{[0-9]+\}$/ { print $1 }' |
      sort -t'{' -k2,2n
  )

  if (( ${#sdma_blocks[@]} == 0 )); then
    DISCOVERY_FAILURES=$((DISCOVERY_FAILURES + 1))
    printf '[ERROR] gpu=%s: no SDMA blocks discovered\n' "$gpu_id" >>"$LOG_FILE"
    echo "WARNING: GPU Node ID $gpu_id has no discovered SDMA blocks" >&2
    continue
  fi

  echo "GPU Node ID $gpu_id: AMD-SMI=${AMD_INDEX_BY_UMR[$umr_instance]} BDF=$bdf SDMA_instances=${#sdma_blocks[@]}"

  for block in "${sdma_blocks[@]}"; do
    BLOCKS=$((BLOCKS + 1))
    umr_args=(-i "$umr_instance" -vmp "$VMP" -O quiet,bits)

    for register in "${REGISTERS[@]}"; do
      # UMR sdma0445 names PREEMPT without the RB_ infix. Keep the requested
      # names in REGISTERS/log metadata, but read the actual database names.
      umr_register="$register"
      case "$register" in
        SDMA_GFX_RB_PREEMPT)
          umr_register=SDMA_GFX_PREEMPT
          ;;
        SDMA_PAGE_RB_PREEMPT)
          umr_register=SDMA_PAGE_PREEMPT
          ;;
        SDMA_RLC[0-7]_RB_PREEMPT)
          umr_register="${register/_RB_PREEMPT/_PREEMPT}"
          ;;
      esac

      path="${block}.reg${umr_register}"
      umr_args+=(-r "$path")
    done

    TOTAL=$((TOTAL + ${#REGISTERS[@]}))
    output=""
    rc=0
    output="$(run_umr "${umr_args[@]}" 2>&1)" || rc=$?
    # Some UMR register reads expand to multiple "=>" lines, so output-line
    # counts overstate success. Count explicit path failures against requests.
    block_failed="$(grep -c '\[ERROR\]: Path .* not found' <<<"$output" || true)"
    if (( block_failed > ${#REGISTERS[@]} )); then
      block_failed=${#REGISTERS[@]}
    fi
    if (( rc != 0 && block_failed == 0 )); then
      block_failed=${#REGISTERS[@]}
    fi
    block_success=$((${#REGISTERS[@]} - block_failed))
    SUCCESS=$((SUCCESS + block_success))
    FAILED=$((FAILED + block_failed))

    {
      printf '=== gpu=%s block=%s registers=%d ===\n' \
        "$gpu_id" "$block" "${#REGISTERS[@]}"
      printf '# command:'
      printf ' %q' "$UMR" "${umr_args[@]}"
      printf '\n'
      printf '%s\n' "$output"
      printf 'block_summary: total=%d successful=%d failed=%d umr_rc=%d\n\n' \
        "${#REGISTERS[@]}" "$block_success" "$block_failed" "$rc"
    } >>"$LOG_FILE"
  done
done

{
  echo "# summary"
  echo "# gpu_count=${#SORTED_UMR_INSTANCES[@]}"
  echo "# sdma_block_count=$BLOCKS"
  echo "# discovery_failures=$DISCOVERY_FAILURES"
  echo "# total_reads=$TOTAL"
  echo "# successful_reads=$SUCCESS"
  echo "# failed_reads=$FAILED"
} >>"$LOG_FILE"

# dmesg remains the final section of the log.
DMESG_RC=127
DMESG_OUTPUT=""
if [[ -n "$DMESG" && -x "$DMESG" ]]; then
  DMESG_RC=0
  DMESG_OUTPUT="$(run_privileged "$DMESG" --color=never --time-format iso 2>&1)" || DMESG_RC=$?
fi

{
  echo
  echo "# dmesg snapshot (final log section)"
  echo "# exit_code=$DMESG_RC"
  if [[ -n "$DMESG_OUTPUT" ]]; then
    printf '%s\n' "$DMESG_OUTPUT"
  else
    echo "[ERROR] dmesg unavailable or produced no output"
  fi
} >>"$LOG_FILE"

printf 'Done: total=%d successful=%d failed=%d\nLog: %s\n' \
  "$TOTAL" "$SUCCESS" "$FAILED" "$LOG_FILE"

if (( SUCCESS == 0 )); then
  exit 1
fi

if (( FAILED > 0 )); then
  echo "WARNING: some registers were unavailable; details are recorded in the log" >&2
  exit 2
fi

if (( DISCOVERY_FAILURES > 0 )); then
  echo "WARNING: SDMA block discovery failed on one or more GPUs" >&2
  exit 2
fi
