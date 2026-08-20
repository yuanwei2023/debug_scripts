#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added Docker AMD runtime log discovery and packaging.
#
# Find Docker containers running torch_server.bin, copy /tmp/amd.log from each
# matching container, and package the captured logs into one .tgz archive.

set -Eeuo pipefail

PROCESS_NAME="${PROCESS_NAME:-torch_server.bin}"
LOG_PATH="${LOG_PATH:-/tmp/amd.log}"
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
OUTPUT_ROOT="${OUTPUT_ROOT:-${DEFAULT_LOG_ROOT}}"
EXPLICIT_PID=""
EXPLICIT_CONTAINER=""

usage() {
  cat <<'EOF'
Usage: ./09_collect_docker_amd_log.sh [options]

Find Docker containers running torch_server.bin, copy /tmp/amd.log from every
matching container, and create a compressed .tgz archive.

Options:
  --pid PID             Select one host PID instead of automatic discovery
  --process-name NAME   Process executable name (default: torch_server.bin)
  --container ID|NAME   Only inspect one Docker container
  --log-path PATH       Log path inside the container (default: /tmp/amd.log)
  -o, --output-dir DIR  Output root (default: /tmp/debug-tools-<UID>)
  -h, --help            Show this help

The collector records AMD_LOG_LEVEL, AMD_LOG_MASK, and AMD_LOG_LEVEL_FILE when
they are readable. It warns if AMD_LOG_LEVEL=4 or the expected log path cannot
be verified, but still copies an existing log to avoid losing diagnostics.

Examples:
  ./09_collect_docker_amd_log.sh
  ./09_collect_docker_amd_log.sh --container my-container
  ./09_collect_docker_amd_log.sh --pid 138732
  ./09_collect_docker_amd_log.sh -o /var/tmp/gpu-debug
EOF
}

require_value() {
  if (($# < 2)) || [[ -z "$2" ]]; then
    echo "[docker-amd-log] option $1 requires a value" >&2
    exit 2
  fi
}

while (($# > 0)); do
  case "$1" in
    --pid)
      require_value "$@"
      EXPLICIT_PID="$2"
      shift 2
      ;;
    --process-name)
      require_value "$@"
      PROCESS_NAME="$2"
      shift 2
      ;;
    --container)
      require_value "$@"
      EXPLICIT_CONTAINER="$2"
      shift 2
      ;;
    --log-path)
      require_value "$@"
      LOG_PATH="$2"
      shift 2
      ;;
    -o|--output-dir)
      require_value "$@"
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[docker-amd-log] unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -n "$EXPLICIT_PID" && ! "$EXPLICIT_PID" =~ ^[1-9][0-9]*$ ]]; then
  echo "[docker-amd-log] PID must be a positive integer: ${EXPLICIT_PID}" >&2
  exit 2
fi
if [[ -z "$PROCESS_NAME" ]]; then
  echo "[docker-amd-log] process name must not be empty" >&2
  exit 2
fi
if [[ "$LOG_PATH" != /* ]]; then
  echo "[docker-amd-log] container log path must be absolute: ${LOG_PATH}" >&2
  exit 2
fi

DOCKER="$(command -v docker 2>/dev/null || true)"
TAR="$(command -v tar 2>/dev/null || true)"
if [[ -z "$DOCKER" ]]; then
  echo "[docker-amd-log] docker command was not found" >&2
  exit 127
fi
if [[ -z "$TAR" ]]; then
  echo "[docker-amd-log] tar command was not found" >&2
  exit 127
fi
if ! "$DOCKER" info >/dev/null 2>&1; then
  echo "[docker-amd-log] cannot access the Docker daemon; check Docker permissions" >&2
  exit 1
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
HOSTNAME_SAFE="$(hostname 2>/dev/null || printf 'unknown-host')"
HOSTNAME_SAFE="${HOSTNAME_SAFE//[^a-zA-Z0-9_.-]/_}"
RUN_DIR="${OUTPUT_ROOT}/docker_amd_log_${HOSTNAME_SAFE}_${TIMESTAMP}"
ARCHIVE_PATH="${RUN_DIR}.tgz"
mkdir -p "$RUN_DIR"

log() {
  printf '[docker-amd-log] %s\n' "$*"
}

declare -A TARGET_PID_SET=()
declare -A MATCHED_PIDS=()
declare -A MATCHED_CONTAINER_SET=()
TARGET_PID_COUNT=0

add_target_pid() {
  local pid="$1"
  if [[ -z "${TARGET_PID_SET[$pid]:-}" ]]; then
    TARGET_PID_SET["$pid"]=1
    ((TARGET_PID_COUNT += 1))
  fi
}

append_matched_pid() {
  local container_id="$1"
  local pid="$2"
  local existing="${MATCHED_PIDS[$container_id]:-}"
  local item

  for item in $existing; do
    [[ "$item" == "$pid" ]] && return 0
  done
  MATCHED_PIDS["$container_id"]="${existing}${existing:+ }${pid}"
}

AMD_SMI="$(command -v amd-smi 2>/dev/null || true)"
AMD_SMI_LOG="${RUN_DIR}/amd-smi-process-discovery.txt"
if [[ -n "$EXPLICIT_PID" ]]; then
  add_target_pid "$EXPLICIT_PID"
  printf 'Explicit host PID: %s\n' "$EXPLICIT_PID" >"$AMD_SMI_LOG"
elif [[ -n "$AMD_SMI" ]]; then
  log "discovering ${PROCESS_NAME} PIDs with amd-smi"
  if "$AMD_SMI" >"$AMD_SMI_LOG" 2>&1; then
    while IFS= read -r pid; do
      [[ "$pid" =~ ^[1-9][0-9]*$ ]] && add_target_pid "$pid"
    done < <(
      awk -v process_name="$PROCESS_NAME" '
        $1 == "|" &&
        $3 ~ /^[1-9][0-9]*$/ &&
        $4 == process_name {
          print $3
        }
      ' "$AMD_SMI_LOG" | sort -nu
    )
  else
    log "warning: amd-smi discovery failed; falling back to Docker process names"
  fi
else
  printf 'amd-smi is unavailable; using Docker process-name discovery\n' \
    >"$AMD_SMI_LOG"
fi

CONTAINER_IDS=()
if [[ -n "$EXPLICIT_CONTAINER" ]]; then
  if ! container_id="$("$DOCKER" inspect --format '{{.Id}}' "$EXPLICIT_CONTAINER" 2>/dev/null)"; then
    echo "[docker-amd-log] Docker container was not found: ${EXPLICIT_CONTAINER}" >&2
    exit 3
  fi
  if [[ "$("$DOCKER" inspect --format '{{.State.Running}}' "$container_id")" != "true" ]]; then
    echo "[docker-amd-log] Docker container is not running: ${EXPLICIT_CONTAINER}" >&2
    exit 3
  fi
  CONTAINER_IDS+=("$container_id")
else
  while IFS= read -r container_id; do
    [[ -n "$container_id" ]] && CONTAINER_IDS+=("$container_id")
  done < <("$DOCKER" ps --no-trunc --quiet)
fi

if ((${#CONTAINER_IDS[@]} == 0)); then
  echo "[docker-amd-log] no running Docker containers were found" >&2
  exit 3
fi

process_line_matches_name() {
  local comm="$1"
  local args="$2"
  local executable="${args%% *}"

  executable="${executable##*/}"
  [[ "$comm" == "$PROCESS_NAME" || "$executable" == "$PROCESS_NAME" ]]
}

log "checking ${#CONTAINER_IDS[@]} running Docker container(s)"
for container_id in "${CONTAINER_IDS[@]}"; do
  top_log="${RUN_DIR}/docker-top-${container_id:0:12}.txt"
  if ! "$DOCKER" top "$container_id" -eo pid=,comm=,args= >"$top_log" 2>&1; then
    log "warning: docker top failed for ${container_id:0:12}"
    continue
  fi

  while read -r host_pid comm args; do
    [[ "$host_pid" =~ ^[1-9][0-9]*$ ]] || continue

    selected=0
    if ((TARGET_PID_COUNT > 0)); then
      [[ -n "${TARGET_PID_SET[$host_pid]:-}" ]] && selected=1
    elif process_line_matches_name "$comm" "$args"; then
      selected=1
    fi

    if ((selected)); then
      MATCHED_CONTAINER_SET["$container_id"]=1
      append_matched_pid "$container_id" "$host_pid"
    fi
  done <"$top_log"
done

if ((${#MATCHED_CONTAINER_SET[@]} == 0)); then
  if ((TARGET_PID_COUNT > 0)); then
    echo "[docker-amd-log] no Docker container owns the selected ${PROCESS_NAME} PID(s)" >&2
  else
    echo "[docker-amd-log] no running Docker container has process ${PROCESS_NAME}" >&2
  fi
  exit 3
fi

COPY_COUNT=0
FAILED_COUNT=0
for container_id in "${!MATCHED_CONTAINER_SET[@]}"; do
  short_id="${container_id:0:12}"
  container_name="$("$DOCKER" inspect --format '{{.Name}}' "$container_id")"
  container_name="${container_name#/}"
  container_image="$("$DOCKER" inspect --format '{{.Config.Image}}' "$container_id")"
  safe_name="${container_name//[^a-zA-Z0-9_.-]/_}"
  container_dir="${RUN_DIR}/container-${safe_name}-${short_id}"
  mkdir -p "$container_dir"

  {
    printf 'capture_time=%s\n' "$(date --iso-8601=seconds)"
    printf 'container_id=%s\n' "$container_id"
    printf 'container_name=%s\n' "$container_name"
    printf 'container_image=%s\n' "$container_image"
    printf 'process_name=%s\n' "$PROCESS_NAME"
    printf 'matched_host_pids=%s\n' "${MATCHED_PIDS[$container_id]}"
    printf 'container_log_path=%s\n' "$LOG_PATH"
    printf 'expected_AMD_LOG_LEVEL=4\n'
    printf 'expected_AMD_LOG_LEVEL_FILE=%s\n' "$LOG_PATH"
  } >"${container_dir}/capture-info.txt"

  env_log="${container_dir}/amd-runtime-environment.txt"
  : >"$env_log"
  verified_process_env=0
  for host_pid in ${MATCHED_PIDS[$container_id]}; do
    printf '[host-pid=%s]\n' "$host_pid" >>"$env_log"
    if [[ -r "/proc/${host_pid}/environ" ]]; then
      while IFS= read -r entry; do
        case "$entry" in
          AMD_LOG_LEVEL=*|AMD_LOG_MASK=*|AMD_LOG_LEVEL_FILE=*)
            printf '%s\n' "$entry" >>"$env_log"
            verified_process_env=1
            ;;
        esac
      done < <(tr '\0' '\n' <"/proc/${host_pid}/environ")
    else
      printf '<process environment is not readable>\n' >>"$env_log"
    fi
  done

  if ((verified_process_env == 0)); then
    printf '[container-config-fallback]\n' >>"$env_log"
    while IFS= read -r entry; do
      case "$entry" in
        AMD_LOG_LEVEL=*|AMD_LOG_MASK=*|AMD_LOG_LEVEL_FILE=*)
          printf '%s\n' "$entry" >>"$env_log"
          ;;
      esac
    done < <(
      "$DOCKER" inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
        "$container_id"
    )
  fi

  if ! awk -F= '$1 == "AMD_LOG_LEVEL" && $2 == "4" { found=1 } END { exit !found }' \
    "$env_log"; then
    log "warning: ${container_name}: AMD_LOG_LEVEL=4 could not be verified"
  fi
  if ! awk -F= -v path="$LOG_PATH" \
    '$1 == "AMD_LOG_LEVEL_FILE" && substr($0, index($0, "=") + 1) == path {
       found=1
     }
     END { exit !found }' "$env_log"; then
    log "warning: ${container_name}: AMD_LOG_LEVEL_FILE=${LOG_PATH} could not be verified"
  fi

  log "copying ${container_name}:${LOG_PATH}"
  if "$DOCKER" cp "${container_id}:${LOG_PATH}" "${container_dir}/amd.txt" \
    >"${container_dir}/docker-cp.txt" 2>&1; then
    if [[ -f "${container_dir}/amd.txt" ]]; then
      log_size="$(stat -c '%s' "${container_dir}/amd.txt")"
      printf 'copied_log_size_bytes=%s\n' "$log_size" \
        >>"${container_dir}/capture-info.txt"
      if command -v sha256sum >/dev/null 2>&1; then
        (
          cd "$container_dir"
          sha256sum amd.txt >amd.txt.sha256
        )
      fi
      ((COPY_COUNT += 1))
    else
      printf 'copy_error=docker cp did not create a regular file\n' \
        >>"${container_dir}/capture-info.txt"
      ((FAILED_COUNT += 1))
    fi
  else
    printf 'copy_error=failed to copy %s\n' "$LOG_PATH" \
      >>"${container_dir}/capture-info.txt"
    ((FAILED_COUNT += 1))
    log "warning: failed to copy ${container_name}:${LOG_PATH}"
  fi
done

{
  printf 'capture_directory=%s\n' "$RUN_DIR"
  printf 'archive=%s\n' "$ARCHIVE_PATH"
  printf 'matched_containers=%s\n' "${#MATCHED_CONTAINER_SET[@]}"
  printf 'logs_copied=%s\n' "$COPY_COUNT"
  printf 'copy_failures=%s\n' "$FAILED_COUNT"
} >"${RUN_DIR}/manifest.txt"

log "creating archive ${ARCHIVE_PATH}"
"$TAR" -C "$(dirname "$RUN_DIR")" -czf "$ARCHIVE_PATH" "$(basename "$RUN_DIR")"

log "capture directory: ${RUN_DIR}"
log "archive: ${ARCHIVE_PATH}"
if ((COPY_COUNT == 0)); then
  echo "[docker-amd-log] no AMD log was copied; inspect the archive metadata" >&2
  exit 4
fi
