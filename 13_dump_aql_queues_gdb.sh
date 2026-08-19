#!/usr/bin/env bash
#
# Attach plain gdb to a live process and dump AQL/SDMA queue state via
# aql_packet_decode_gdb.py. Does not modify the target runtime environment.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GDB_SCRIPT="${SCRIPT_DIR}/aql_packet_decode_gdb.py"
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
PID=""
OUTPUT_DIR=""
OUTPUT_FILE=""
GDB_BIN="${GDB:-/usr/bin/gdb}"
GDB_TIMEOUT="${GDB_TIMEOUT:-120}"
NO_SUDO=0
SOLIB_SEARCH_PATH="${SOLIB_SEARCH_PATH:-}"
DEBUG_FILE_DIRECTORY="${DEBUG_FILE_DIRECTORY:-}"

usage() {
  cat <<'EOF'
Usage: ./13_dump_aql_queues_gdb.sh [options]

Attach GDB with Python support to a live process and decode HSA/SDMA queues by
sourcing aql_packet_decode_gdb.py. This script only performs the gdb dump; it
does not start workloads or change ROCm/HIP runtime settings. No specific GDB
version is required or checked.

ROCr debuginfo prerequisite:
  Install an hsa-rocr-debuginfo RPM whose version exactly matches the
  installed/mapped hsa-rocr runtime. Example only (use the matching version):
    sudo dnf install /path/to/hsa-rocr-debuginfo-1.18.0.70204-93.el8.x86_64.rpm

Options:
  --pid PID                 Target process ID (required)
  -o, --output-dir DIR      Output directory (default: timestamped under /tmp)
  --output-file PATH        Explicit output file path (overrides --output-dir)
  --gdb PATH                GDB with Python support (default: /usr/bin/gdb or $GDB)
  --timeout SEC             Attach/decode timeout in seconds (default: 120)
  --solib-search-path PATH  Optional gdb solib-search-path
  --debug-file-directory D  Optional gdb debug-file-directory
  --no-sudo                 Do not relaunch with sudo when ptrace is denied
  -h, --help                Show this help

Examples:
  ./13_dump_aql_queues_gdb.sh --pid 875176
  ./13_dump_aql_queues_gdb.sh --pid=875176 -o /tmp/queue_dump

Equivalent manual command:
  /usr/bin/gdb -batch -p 875176 \
    -ex "source aql_packet_decode_gdb.py" \
    -ex "detach"

Environment:
  GDB, GDB_TIMEOUT, SOLIB_SEARCH_PATH, DEBUG_FILE_DIRECTORY,
  DEBUG_TOOLS_TMP_DIR, AQL_PACKET_DECODE_OUTPUT
EOF
}

log() {
  printf '[aql-queue-dump] %s\n' "$*"
}

die() {
  printf '[aql-queue-dump] ERROR: %s\n' "$*" >&2
  exit 2
}

require_value() {
  if (($# < 2)) || [[ -z "$2" ]]; then
    die "Option $1 requires a value"
  fi
}

validate_pid() {
  if [[ ! "$PID" =~ ^[1-9][0-9]*$ ]]; then
    die "--pid must be a positive integer, got: ${PID}"
  fi
}

has_python_gdb() {
  "$1" -q -batch -ex "python print('AQL_QUEUE_DUMP_PYTHON_OK')" 2>&1 \
    | awk '/AQL_QUEUE_DUMP_PYTHON_OK/{found=1} END{exit !found}'
}

select_gdb() {
  if [[ -n "${GDB_BIN}" && -x "${GDB_BIN}" ]] && has_python_gdb "${GDB_BIN}"; then
    return 0
  fi
  if [[ -x /usr/bin/gdb ]] && has_python_gdb /usr/bin/gdb; then
    GDB_BIN=/usr/bin/gdb
    return 0
  fi
  local candidate=""
  candidate="$(command -v gdb 2>/dev/null || true)"
  if [[ -n "${candidate}" && -x "${candidate}" ]] && has_python_gdb "${candidate}"; then
    GDB_BIN="${candidate}"
    return 0
  fi
  return 1
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
    -o=*|--output-dir=*)
      OUTPUT_DIR="${1#*=}"
      shift
      ;;
    -o|--output-dir)
      require_value "$@"
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --output-file=*)
      OUTPUT_FILE="${1#*=}"
      shift
      ;;
    --output-file)
      require_value "$@"
      OUTPUT_FILE="$2"
      shift 2
      ;;
    --gdb=*)
      GDB_BIN="${1#*=}"
      shift
      ;;
    --gdb)
      require_value "$@"
      GDB_BIN="$2"
      shift 2
      ;;
    --timeout=*)
      GDB_TIMEOUT="${1#*=}"
      shift
      ;;
    --timeout)
      require_value "$@"
      GDB_TIMEOUT="$2"
      shift 2
      ;;
    --solib-search-path=*)
      SOLIB_SEARCH_PATH="${1#*=}"
      shift
      ;;
    --solib-search-path)
      require_value "$@"
      SOLIB_SEARCH_PATH="$2"
      shift 2
      ;;
    --debug-file-directory=*)
      DEBUG_FILE_DIRECTORY="${1#*=}"
      shift
      ;;
    --debug-file-directory)
      require_value "$@"
      DEBUG_FILE_DIRECTORY="$2"
      shift 2
      ;;
    --no-sudo)
      NO_SUDO=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1 (try --help)"
      ;;
  esac
done

if [[ -z "$PID" ]]; then
  usage
  die "Missing required option: --pid"
fi

validate_pid
[[ -f "$GDB_SCRIPT" ]] || die "Missing gdb script: ${GDB_SCRIPT}"
[[ -d "/proc/${PID}" ]] || die "PID ${PID} is not running"

if [[ -r "/proc/${PID}/comm" ]]; then
  proc_comm="$(<"/proc/${PID}/comm")"
  log "target PID: ${PID} (${proc_comm})"
else
  log "target PID: ${PID}"
fi

if [[ -z "$OUTPUT_FILE" ]]; then
  if [[ -z "$OUTPUT_DIR" ]]; then
    timestamp="$(date +%Y%m%d_%H%M%S)"
    hostname_short="$(hostname -s 2>/dev/null || hostname)"
    OUTPUT_DIR="${DEFAULT_LOG_ROOT}/aql_queue_dump_${hostname_short}_${timestamp}_pid${PID}"
  fi
  mkdir -p "$OUTPUT_DIR"
  OUTPUT_DIR="$(cd -- "$OUTPUT_DIR" && pwd)"
  OUTPUT_FILE="${OUTPUT_DIR}/aql_packet_decode_gdb.txt"
else
  mkdir -p "$(dirname -- "$OUTPUT_FILE")"
  OUTPUT_FILE="$(cd -- "$(dirname -- "$OUTPUT_FILE")" && pwd)/$(basename -- "$OUTPUT_FILE")"
  OUTPUT_DIR="$(dirname -- "$OUTPUT_FILE")"
fi

amd_smi_log="${OUTPUT_DIR}/amd-smi.log"
amd_smi_bin="${AMD_SMI:-$(command -v amd-smi 2>/dev/null || true)}"
{
  echo "# captured: $(date -Is 2>/dev/null || date)"
  echo "# command: amd-smi"
  echo
  if [[ -n "${amd_smi_bin}" && -x "${amd_smi_bin}" ]]; then
    "${amd_smi_bin}" 2>&1 || echo "[ERROR] amd-smi exited $?"
  else
    echo "[ERROR] amd-smi unavailable"
  fi
} >"${amd_smi_log}"
log "amd-smi: ${amd_smi_log}"

tmp_output="${OUTPUT_DIR}/.aql_packet_decode_gdb.${PID}.$$.tmp"
gdb_log="${OUTPUT_DIR}/.aql_packet_decode_gdb.${PID}.$$.gdb.log"
cleanup() {
  rm -f -- "${tmp_output}" "${gdb_log}"
}
trap cleanup EXIT

select_gdb || die "GDB with Python support is required (set GDB or --gdb)"
log "debugger: ${GDB_BIN}"
log "output: ${OUTPUT_FILE}"

gdb_args=(-q -batch -p "${PID}" -ex "set confirm off")
if [[ -n "$SOLIB_SEARCH_PATH" ]]; then
  gdb_args+=(-ex "set solib-search-path ${SOLIB_SEARCH_PATH}")
fi
if [[ -n "$DEBUG_FILE_DIRECTORY" ]]; then
  [[ -d "$DEBUG_FILE_DIRECTORY" ]] || die "Not a directory: ${DEBUG_FILE_DIRECTORY}"
  gdb_args+=(-ex "set debug-file-directory ${DEBUG_FILE_DIRECTORY}")
fi
gdb_args+=(-ex "source ${GDB_SCRIPT}" -ex "detach")

run_gdb() {
  local -a prefix=("$@")
  set +e
  timeout --foreground -k 5s "${GDB_TIMEOUT}s" \
    "${prefix[@]}" "${GDB_BIN}" "${gdb_args[@]}" 2>&1 | tee "${gdb_log}"
  local rc=${PIPESTATUS[0]}
  set -e
  return "${rc}"
}

pid_uid="$(stat -c '%u' "/proc/${PID}")"
pid_owner="$(stat -c '%U' "/proc/${PID}")"
log "attaching (timeout ${GDB_TIMEOUT}s)"

gdb_rc=0
if [[ "${EUID}" -eq 0 || "${EUID}" -eq "${pid_uid}" ]]; then
  run_gdb env \
    "AQL_PACKET_DECODE_OUTPUT=${tmp_output}" \
    "AQL_PACKET_DECODE_DISPLAY_OUTPUT=${OUTPUT_FILE}" || gdb_rc=$?
else
  ((NO_SUDO == 1)) && die "Target belongs to ${pid_owner}; rerun with sudo or drop --no-sudo"
  command -v sudo >/dev/null || die "sudo is required to attach to ${pid_owner}'s process"
  sudo -n true 2>/dev/null \
    || die "passwordless sudo is required for noninteractive attach"
  run_gdb sudo -n env \
    "AQL_PACKET_DECODE_OUTPUT=${tmp_output}" \
    "AQL_PACKET_DECODE_DISPLAY_OUTPUT=${OUTPUT_FILE}" || gdb_rc=$?
fi

if [[ "${gdb_rc}" -ne 0 ]] \
  && [[ "${EUID}" -ne 0 ]] \
  && [[ "${EUID}" -eq "${pid_uid}" ]] \
  && ((NO_SUDO == 0)) \
  && grep -Eq 'ptrace: Operation not permitted|Could not attach|Permission denied' "${gdb_log}" \
  && command -v sudo >/dev/null \
  && sudo -n true 2>/dev/null
then
  log "direct attach denied; retrying with sudo"
  : >"${gdb_log}"
  gdb_rc=0
  run_gdb sudo -n env \
    "AQL_PACKET_DECODE_OUTPUT=${tmp_output}" \
    "AQL_PACKET_DECODE_DISPLAY_OUTPUT=${OUTPUT_FILE}" || gdb_rc=$?
fi

[[ "${gdb_rc}" -eq 124 || "${gdb_rc}" -eq 137 ]] && die "GDB timed out after ${GDB_TIMEOUT}s"
[[ "${gdb_rc}" -eq 0 ]] || die "GDB failed (exit ${gdb_rc}); see ${gdb_log}"
[[ -s "${tmp_output}" ]] || die "Decoder produced no output; see ${gdb_log}"

mv -f -- "${tmp_output}" "${OUTPUT_FILE}"
if grep -Eq '^Enumerated [1-9][0-9]* queue\(s\):' "${OUTPUT_FILE}"; then
  log "$(grep -Em1 '^Enumerated [1-9][0-9]* queue\(s\):' "${OUTPUT_FILE}")"
elif grep -Eq 'DW_FORM_strx|Dwarf Error|DWARF.*Error' "${gdb_log}"; then
  die "GDB reported a DWARF error and queue enumeration failed; see ${gdb_log}"
else
  log "WARNING: no queue enumeration in output; inspect ${OUTPUT_FILE}"
fi

log "done: ${OUTPUT_FILE}"
printf '%s\n' "${OUTPUT_FILE}"
