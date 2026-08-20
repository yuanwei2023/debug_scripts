#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

MOCK_BIN="${TEMP_DIR}/bin"
MOCK_UMR="${MOCK_BIN}/umr"
mkdir -p "$MOCK_BIN"

cat >"$MOCK_UMR" <<'EOF'
#!/usr/bin/env bash
set -u

printf '%q' "$1" >>"$MOCK_UMR_LOG"
if (($# > 1)); then
  printf ' %q' "${@:2}" >>"$MOCK_UMR_LOG"
fi
printf '\n' >>"$MOCK_UMR_LOG"

if [[ "${1:-}" == "--help" ]]; then
  if [[ "$MOCK_UMR_MODE" == "tool" ]]; then
    printf '%s\n' \
      'User Mode Register debugger v1.0.11' \
      '  --tool [cpc, cpg, sdma]' \
      '    Invoke diagnostic tools.'
  else
    printf '%s\n' \
      'User Mode Register debugger v1.0.10' \
      '  --print-cpc, -cpc' \
      '    Print CPC register data.'
  fi
  exit 0
fi

if [[ "${1:-}" == "--script" && "${2:-}" == "instances" ]]; then
  printf '128\n'
  exit 0
fi
if [[ "${1:-}" == "--script" && "${2:-}" == "xcds" ]]; then
  printf '%s\n' '-1'
  exit 0
fi

case " $* " in
  *" --tool cpc -- -O no_backtrace "*)
    [[ "$MOCK_UMR_MODE" == "tool" ]] || {
      echo '[WARNING]: --tool was not understood' >&2
      exit 2
    }
    printf 'MOCK CPC REGISTER DATA (tool)\n'
    ;;
  *" --print-cpc -O no_backtrace "*)
    [[ "$MOCK_UMR_MODE" == "legacy" ]] || {
      echo '[WARNING]: --print-cpc was not understood' >&2
      exit 2
    }
    printf 'MOCK CPC REGISTER DATA (legacy)\n'
    ;;
  *" -wa none "*)
    printf 'MOCK WAVE DATA\n'
    ;;
  *)
    echo "[WARNING]: unexpected mock UMR arguments: $*" >&2
    exit 2
    ;;
esac
EOF
chmod +x "$MOCK_UMR"

cat >"${MOCK_BIN}/journalctl" <<'EOF'
#!/usr/bin/env bash
printf 'mock kernel journal\n'
EOF
chmod +x "${MOCK_BIN}/journalctl"

run_case() {
  local mode="$1"
  local output_dir="${TEMP_DIR}/${mode}-output"
  local invocation_log="${TEMP_DIR}/${mode}-umr-invocations.txt"
  local stdout_file="${TEMP_DIR}/${mode}-stdout.txt"
  local stderr_file="${TEMP_DIR}/${mode}-stderr.txt"
  local rc
  local expected_command
  local cpc_files

  : >"$invocation_log"
  set +e
  MOCK_UMR_MODE="$mode" MOCK_UMR_LOG="$invocation_log" \
    PATH="${MOCK_BIN}:$PATH" \
    bash "${SCRIPT_DIR}/05_dump_all_cpc_info.sh" \
      --no-sudo --skip-halt-check --umr "$MOCK_UMR" \
      --output "$output_dir" >"$stdout_file" 2>"$stderr_file"
  rc=$?
  set -e
  if ((rc != 0 && rc != 2)); then
    echo "${mode}: collector failed unexpectedly with rc=${rc}" >&2
    return 1
  fi

  if [[ "$(grep -c '^--help$' "$invocation_log")" -ne 1 ]]; then
    echo "${mode}: expected exactly one UMR help capability probe" >&2
    return 1
  fi
  if [[ "$mode" == "tool" ]]; then
    expected_command='-i 128 -vmp -1 --tool cpc -- -O no_backtrace'
  else
    expected_command='-i 128 -vmp -1 --print-cpc -O no_backtrace'
  fi
  grep -Fqx -- "$expected_command" "$invocation_log"

  cpc_files=("${output_dir}"/*_umr_cpc_gpu0.txt)
  if ((${#cpc_files[@]} != 1)) || [[ ! -s "${cpc_files[0]}" ]]; then
    echo "${mode}: CPC output was not captured" >&2
    return 1
  fi
  grep -Fq "MOCK CPC REGISTER DATA (${mode})" "${cpc_files[0]}"
  if grep -q 'Command line argument\|was not understood' \
    "$stdout_file" "$stderr_file" "${output_dir}"/*_errors.txt; then
    echo "${mode}: UMR argument warning leaked into the capture" >&2
    return 1
  fi
  grep -Fq "UMR CPC command mode: ${mode}" "$stdout_file"
  grep -Fq "umr_cpc_command_mode=${mode}" "${output_dir}"/*_summary.txt
}

run_case tool
run_case legacy

echo "mocked UMR CPC compatibility test passed"
