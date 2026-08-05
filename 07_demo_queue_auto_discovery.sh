#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added the active multi-GPU queue auto-discovery demo.
#   2026-08-03 - Kept this compatibility entry after moving tests under tests/.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

exec "${SCRIPT_DIR}/tests/run_queue_auto_discovery_test.sh" "$@"
