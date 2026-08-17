#!/usr/bin/env bash
#
# Capture bare amd-smi output into a log file.

capture_amd_smi_log() {
  local dest="$1"
  local amd_smi="${AMD_SMI:-$(command -v amd-smi 2>/dev/null || true)}"

  mkdir -p "$(dirname -- "${dest}")"
  {
    echo "# captured: $(date -Is 2>/dev/null || date)"
    echo "# command: amd-smi"
    echo
    if [[ -n "${amd_smi}" && -x "${amd_smi}" ]]; then
      "${amd_smi}" 2>&1 || echo "[ERROR] amd-smi exited $?"
    else
      echo "[ERROR] amd-smi unavailable"
    fi
  } >"${dest}"
}
