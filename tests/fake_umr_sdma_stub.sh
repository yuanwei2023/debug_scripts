#!/usr/bin/env bash
# Fake UMR for offline 03_dump_sdma_registers.sh tests (no hardware access).

set -u

FAKE_UMR_VERSION="${FAKE_UMR_VERSION:-1.0.11}"
FAKE_UMR_INSTANCES="${FAKE_UMR_INSTANCES:-1 2 3}"
FAKE_UMR_BLOCKS_PER_GPU="${FAKE_UMR_BLOCKS_PER_GPU:-2}"
FAKE_UMR_SLEEP_SEC="${FAKE_UMR_SLEEP_SEC:-0}"
FAKE_UMR_FAIL_INSTANCE="${FAKE_UMR_FAIL_INSTANCE:-}"
FAKE_UMR_HANG_LB_SEC="${FAKE_UMR_HANG_LB_SEC:-0}"
FAKE_UMR_HANG_READ_SEC="${FAKE_UMR_HANG_READ_SEC:-0}"
FAKE_UMR_MISSING_TOKEN="${FAKE_UMR_MISSING_TOKEN:-}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "User Mode Register debugger v${FAKE_UMR_VERSION} for AMDGPU devices (build: ${FAKE_UMR_VERSION}-test), Copyright (c) 2025, AMD Inc."
  echo "*** Device Selection ***"
  echo "  -i, --instance N"
  echo "  -lb, --list-blocks"
  echo "  -vmp, --vm-partition N"
  echo "  -O, --options OPTS"
  echo "  -r, --read PATH"
  echo "  --script CMD"
  if [[ -n "$FAKE_UMR_MISSING_TOKEN" ]]; then
    exit 0
  fi
  exit 0
fi

if [[ "${1:-}" == "--script" ]]; then
  case "${2:-}" in
    instances)
      echo "$FAKE_UMR_INSTANCES"
      exit 0
      ;;
    "")
      echo "Available script commands: instances"
      exit 0
      ;;
  esac
  echo "unknown script command: ${2:-}" >&2
  exit 1
fi

instance=""
mode=""
declare -a read_paths=()

while (( $# > 0 )); do
  case "$1" in
    -i|--instance)
      instance="$2"
      shift 2
      ;;
    -lb|--list-blocks)
      mode=list-blocks
      shift
      ;;
    -vmp|--vm-partition)
      shift 2
      ;;
    -O|--options)
      shift 2
      ;;
    -r|--read)
      read_paths+=("$2")
      mode=read
      shift 2
      ;;
    *)
      echo "fake umr: unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

if [[ "$mode" == "list-blocks" ]]; then
  if [[ -n "$FAKE_UMR_HANG_LB_SEC" && "$FAKE_UMR_HANG_LB_SEC" != "0" ]]; then
    sleep "$FAKE_UMR_HANG_LB_SEC"
  fi
  if [[ "$instance" == "$FAKE_UMR_FAIL_INSTANCE" ]]; then
    echo "[ERROR] fake umr: list-blocks forced failure for instance $instance" >&2
    exit 1
  fi
  for (( b = 0; b < FAKE_UMR_BLOCKS_PER_GPU; b++ )); do
    printf '\tfake.sdma0445{%d} (4.4.5)\n' "$b"
  done
  exit 0
fi

if [[ "$mode" == "read" ]]; then
  if [[ -n "$FAKE_UMR_SLEEP_SEC" && "$FAKE_UMR_SLEEP_SEC" != "0" ]]; then
    sleep "$FAKE_UMR_SLEEP_SEC"
  fi
  if [[ -n "$FAKE_UMR_HANG_READ_SEC" && "$FAKE_UMR_HANG_READ_SEC" != "0" ]]; then
    sleep "$FAKE_UMR_HANG_READ_SEC"
  fi
  if [[ "$instance" == "$FAKE_UMR_FAIL_INSTANCE" ]]; then
    echo "[ERROR] fake umr: batched read forced failure for instance $instance" >&2
    exit 1
  fi
  for path in "${read_paths[@]}"; do
    if [[ "$path" != *".mmSDMA0_"* ]]; then
      echo "[ERROR]: Path <${path}> uses an invalid fake SDMA register namespace"
      echo "  #00 ?? at ??:0"
    elif [[ "$path" == *".mmSDMA0_FAIL_REG" ]]; then
      echo "[ERROR]: Path <${path}> not found on this ASIC"
      echo "  #00 ?? at ??:0"
    else
      echo "${path} => 0xdeadbeef"
    fi
  done
  exit 0
fi

echo "fake umr: no operation selected" >&2
exit 1
