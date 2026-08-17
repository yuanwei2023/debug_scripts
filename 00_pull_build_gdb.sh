#!/usr/bin/env bash
#
# Build upstream GNU GDB with Python 3.11 into a local prefix.
# Use the result via: export GDB=/path/to/prefix/bin/gdb

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
GDB_VERSION="${GDB_VERSION:-14.2}"
GDB_PREFIX="${GDB_PREFIX:-${WORKSPACE_ROOT}/tools/gdb-${GDB_VERSION}}"
GDB_SRC_DIR="${GDB_SRC_DIR:-${WORKSPACE_ROOT}/src/gdb-${GDB_VERSION}}"
GDB_TARBALL="${GDB_TARBALL:-gdb-${GDB_VERSION}.tar.xz}"
GDB_URL="${GDB_URL:-https://ftp.gnu.org/gnu/gdb/${GDB_TARBALL}}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3.11}"
JOBS="${JOBS:-$(nproc)}"
BASHRC_FILE="${BASHRC_FILE:-${HOME}/.bashrc}"
SKIP_PULL=0
FORCE_REBUILD=0
INSTALL_BASHRC=0
GDB_EXPORT_LINE=""

log() { printf '[gdb-build] %s\n' "$*" >&2; }
die() { printf '[gdb-build] ERROR: %s\n' "$*" >&2; exit 2; }

gdb_export_line() {
  printf 'export GDB=%s/bin/gdb' "${GDB_PREFIX}"
}

print_gdb_env_hint() {
  GDB_EXPORT_LINE="$(gdb_export_line)"
  cat <<EOF >&2

Optional: use the built GDB for AQL queue dumps (scripts 06/13):

  ${GDB_EXPORT_LINE}

For the current shell only:

  ${GDB_EXPORT_LINE}

To persist across logins, append the line above to ~/.bashrc manually, or rerun:

  ./00_pull_build_gdb.sh --install-bashrc

Upgrade later with --force-rebuild (same prefix by default).
EOF
}

install_bashrc_export() {
  local line marker
  line="$(gdb_export_line)"
  marker="# rocm-forensics GDB (00_pull_build_gdb.sh)"

  if [[ -f "${BASHRC_FILE}" ]] && grep -Fq "${line}" "${BASHRC_FILE}"; then
    log "GDB export already present in ${BASHRC_FILE}"
    return 0
  fi

  {
    printf '\n%s\n' "${marker}"
    printf '%s\n' "${line}"
  } >>"${BASHRC_FILE}"

  log "appended GDB export to ${BASHRC_FILE}"
  log "run: source ${BASHRC_FILE}"
}

finish() {
  local gdb_bin="${GDB_PREFIX}/bin/gdb"
  log "done: ${gdb_bin}"
  "${gdb_bin}" --version | head -1 >&2
  print_gdb_env_hint
  if [[ "${INSTALL_BASHRC}" -eq 1 ]]; then
    install_bashrc_export
  fi
}

usage() {
  cat <<EOF
Usage: ./00_pull_build_gdb.sh [options]

Build upstream GNU GDB ${GDB_VERSION} with Python support into:
  ${GDB_PREFIX}

Options:
  --prefix PATH        Install prefix (default: tools/gdb-${GDB_VERSION})
  --python PATH        Python interpreter (default: /usr/bin/python3.11)
  --skip-pull          Reuse existing source tree under src/
  --force-rebuild      Reconfigure and rebuild even if gdb already exists
  --install-bashrc     Append "export GDB=..." to ~/.bashrc if not present
  --bashrc-file PATH   Bashrc target (default: ~/.bashrc)
  -h, --help           Show this help

After build (optional; scripts 06/13 still default to system GDB):
  export GDB=${GDB_PREFIX}/bin/gdb
  ./13_dump_aql_queues_gdb.sh --pid=PID
EOF
}

while (($# > 0)); do
  case "$1" in
    --prefix=*)
      GDB_PREFIX="${1#*=}"
      shift
      ;;
    --prefix)
      GDB_PREFIX="$2"
      shift 2
      ;;
    --python=*)
      PYTHON_BIN="${1#*=}"
      shift
      ;;
    --python)
      PYTHON_BIN="$2"
      shift 2
      ;;
    --skip-pull)
      SKIP_PULL=1
      shift
      ;;
    --force-rebuild)
      FORCE_REBUILD=1
      shift
      ;;
    --install-bashrc)
      INSTALL_BASHRC=1
      shift
      ;;
    --bashrc-file=*)
      BASHRC_FILE="${1#*=}"
      shift
      ;;
    --bashrc-file)
      BASHRC_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

[[ -x "${PYTHON_BIN}" ]] || die "Python not found: ${PYTHON_BIN}"
if ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)'; then
  die "Python >= 3.8 required: ${PYTHON_BIN}"
fi

if [[ -x "${GDB_PREFIX}/bin/gdb" && "${FORCE_REBUILD}" -eq 0 ]]; then
  log "already built: ${GDB_PREFIX}/bin/gdb"
  finish
  exit 0
fi

mkdir -p "$(dirname -- "${GDB_SRC_DIR}")" "${GDB_PREFIX}"

if [[ "${SKIP_PULL}" -eq 0 || ! -d "${GDB_SRC_DIR}" ]]; then
  tmp_tar="$(mktemp "${TMPDIR:-/tmp}/gdb-${GDB_VERSION}.XXXXXX.tar.xz")"
  log "downloading ${GDB_URL}"
  if command -v curl >/dev/null 2>&1; then
    curl -fL --retry 3 -o "${tmp_tar}" "${GDB_URL}"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "${tmp_tar}" "${GDB_URL}"
  else
    die "curl or wget required to download GDB source"
  fi
  rm -rf "${GDB_SRC_DIR}"
  mkdir -p "${GDB_SRC_DIR}"
  tar -xJf "${tmp_tar}" -C "$(dirname -- "${GDB_SRC_DIR}")"
  rm -f "${tmp_tar}"
fi

build_dir="${GDB_SRC_DIR}/build"
rm -rf "${build_dir}"
mkdir -p "${build_dir}"

log "configure prefix=${GDB_PREFIX} python=${PYTHON_BIN}"
(
  cd "${build_dir}"
  ../configure \
    --prefix="${GDB_PREFIX}" \
    --with-python="${PYTHON_BIN}" \
    --disable-nls \
    --with-system-readline \
    --with-system-gdbinit=/dev/null
)

log "building with -j${JOBS}"
make -C "${build_dir}" -j"${JOBS}"
make -C "${build_dir}" install

log "verifying Python support"
if ! "${GDB_PREFIX}/bin/gdb" -q -batch -ex "python print('AQL_QUEUE_DUMP_PYTHON_OK')" 2>&1 \
  | awk '/AQL_QUEUE_DUMP_PYTHON_OK/{found=1} END{exit !found}'; then
  die "built gdb lacks working Python support"
fi

finish
