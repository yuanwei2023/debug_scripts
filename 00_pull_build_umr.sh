#!/usr/bin/env bash
#
# Author: Perry_Yuan@outlook.com
# Log History:
#   2026-08-03 - Added resilient UMR clone, LLVM fallback, and build logging.
#   2026-08-03 - Refused root-owned UMR source and build installations.

set -euo pipefail

UMR_DIR="${UMR_DIR:-${HOME}/umr}"
UMR_REPOSITORY="${UMR_REPOSITORY:-https://github.com/yuanwei2023/umr.git}"
UMR_REMOTE="${UMR_REMOTE:-origin}"
UMR_BRANCH="${UMR_BRANCH:-308-v2}"
JOBS="${JOBS:-$(nproc)}"
LLVM_PACKAGE="${LLVM_PACKAGE:-llvm-devel}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_LOG_ROOT="${DEBUG_TOOLS_TMP_DIR:-/tmp/debug-tools-${SUDO_UID:-$(id -u)}}"
SKIP_PULL=0
BUILD_PACKAGE=0
INSTALL_DEPS=0
BUILD_EXTRA_ARGS=()

usage() {
  cat <<'EOF'
Usage: ./00_pull_build_umr.sh [options] [-- extra-cmake-options]

Clone or update the selected UMR branch, then build UMR.
LLVM support is enabled when its development files are available. If they are
missing, the script tries yum install and falls back to a no-LLVM build.
Run this script as a regular user; sudo is used only for package installation.

Options:
  --umr-dir DIR      UMR source directory (default: ~/umr)
  --repository URL   Repository to clone when DIR is absent
  --remote NAME      Git remote to pull (default: origin)
  --branch NAME      Git branch to update (default: 308-v2)
  --jobs N           Number of parallel build jobs
  --install-deps     Install common UMR build dependencies with yum
  --package          Build and validate the RPM package
  --skip-pull        Build the current checkout without fetching or pulling
  -h, --help         Show this help

Environment overrides:
  UMR_DIR, UMR_REPOSITORY, UMR_REMOTE, UMR_BRANCH, JOBS, LLVM_DIR,
  LLVM_PACKAGE, LLVM_DOWNLOAD_DIR, LOG_FILE, DEBUG_TOOLS_TMP_DIR

Examples:
  ./00_pull_build_umr.sh
  ./00_pull_build_umr.sh --install-deps
  ./00_pull_build_umr.sh --branch 308-v2 --package
  ./00_pull_build_umr.sh -- -DCMAKE_BUILD_TYPE=Debug
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
    --umr-dir)
      require_value "$@"
      UMR_DIR="$2"
      shift 2
      ;;
    --repository)
      require_value "$@"
      UMR_REPOSITORY="$2"
      shift 2
      ;;
    --remote)
      require_value "$@"
      UMR_REMOTE="$2"
      shift 2
      ;;
    --branch)
      require_value "$@"
      UMR_BRANCH="$2"
      shift 2
      ;;
    --jobs)
      require_value "$@"
      JOBS="$2"
      shift 2
      ;;
    --install-deps)
      INSTALL_DEPS=1
      shift
      ;;
    --package)
      BUILD_PACKAGE=1
      shift
      ;;
    --skip-pull)
      SKIP_PULL=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      BUILD_EXTRA_ARGS=("$@")
      break
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
  echo "JOBS must be a positive integer: ${JOBS}" >&2
  exit 2
fi

if ((EUID == 0)); then
  cat >&2 <<EOF
ERROR: Do not run 00_pull_build_umr.sh as root or with sudo.
UMR source and build artifacts must be owned by a regular user.
Run the script again without sudo; package installation will request sudo
separately when required.
Requested UMR directory: ${UMR_DIR}
EOF
  exit 1
fi

LOG_FILE="${LOG_FILE:-${DEFAULT_LOG_ROOT}/umr_build_$(date +%Y%m%d_%H%M%S).log}"
mkdir -p -- "$(dirname -- "$LOG_FILE")"
if command -v tee >/dev/null 2>&1; then
  exec > >(tee -a "$LOG_FILE") 2>&1
else
  echo "WARNING: tee is unavailable; output will not be saved to ${LOG_FILE}" \
    >&2
fi

log() {
  printf '[umr-build] %s %s\n' "$(date --iso-8601=seconds)" "$*"
}

step_begin() {
  log "BEGIN step=$1"
}

step_end() {
  log "END step=$1 status=$2 rc=$3"
}

step_skip() {
  log "SKIP step=$1 reason=$2"
}

run_step() {
  local name="$1"
  local rc
  shift

  step_begin "$name"
  if "$@"; then
    step_end "$name" "PASS" 0
    return 0
  else
    rc=$?
    step_end "$name" "FAIL" "$rc"
    return "$rc"
  fi
}

on_exit() {
  local rc=$?
  if ((rc == 0)); then
    log "END session status=PASS rc=0 log=${LOG_FILE}"
  else
    log "END session status=FAIL rc=${rc} log=${LOG_FILE}"
  fi
}
trap on_exit EXIT

log "BEGIN session log=${LOG_FILE}"
step_begin "environment"
log "INFO user=$(id -un) euid=${EUID} host=$(hostname)"
log "INFO umr_dir=${UMR_DIR} branch=${UMR_BRANCH} jobs=${JOBS}"
log "INFO llvm_package=${LLVM_PACKAGE} llvm_dir=${LLVM_DIR:-auto}"
if command -v rpm >/dev/null 2>&1; then
  log "INFO installed_rocm_llvm=$(rpm -q rocm-llvm 2>/dev/null || echo none)"
  log "INFO installed_llvm_devel=$(rpm -q llvm-devel 2>/dev/null || echo none)"
fi
step_end "environment" "PASS" 0

step_begin "required_commands"
for command in git cmake; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: ${command}" >&2
    step_end "required_commands" "FAIL" 1
    exit 1
  fi
done
step_end "required_commands" "PASS" 0

run_as_root() {
  if ((EUID == 0)); then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    echo "Root access is required to run: $*" >&2
    return 1
  fi
}

run_yum_install() {
  if ! command -v yum >/dev/null 2>&1; then
    echo "yum is unavailable" >&2
    return 1
  fi
  run_as_root yum install -y "$@"
}

search_yum_package() {
  local package="$1"

  if ! command -v yum >/dev/null 2>&1; then
    echo "yum is unavailable" >&2
    return 1
  fi

  echo "===== yum search ${package} ====="
  yum search "$package" || return
  echo "===== yum list --showduplicates ${package} ====="
  yum list --showduplicates "$package"
}

download_yum_package() {
  local package="$1"
  local download_dir="$2"
  local rpms

  mkdir -p -- "$download_dir"
  run_as_root yum install -y --downloadonly \
    "--downloaddir=${download_dir}" "$package" || return

  shopt -s nullglob
  rpms=("${download_dir}"/*.rpm)
  shopt -u nullglob
  if ((${#rpms[@]} == 0)); then
    echo "No RPMs were downloaded to ${download_dir}" >&2
    return 1
  fi

  echo "Downloaded RPMs:"
  printf '  %s\n' "${rpms[@]}"
}

install_downloaded_rpms() {
  local download_dir="$1"
  local rpms

  shopt -s nullglob
  rpms=("${download_dir}"/*.rpm)
  shopt -u nullglob
  if ((${#rpms[@]} == 0)); then
    echo "No downloaded RPMs found in ${download_dir}" >&2
    return 1
  fi

  run_yum_install "${rpms[@]}"
}

find_llvm_dir() {
  local candidate llvm_config

  if [[ -n "${LLVM_DIR:-}" ]]; then
    if [[ -f "${LLVM_DIR}/LLVMConfig.cmake" ]]; then
      printf '%s\n' "$LLVM_DIR"
      return 0
    fi
    echo "Ignoring invalid LLVM_DIR (LLVMConfig.cmake missing): ${LLVM_DIR}" \
      >&2
  fi

  for llvm_config in \
    /opt/rocm/llvm/bin/llvm-config; do
    if [[ "$llvm_config" == */* ]]; then
      [[ -x "$llvm_config" ]] || continue
    fi
    candidate="$("$llvm_config" --cmakedir 2>/dev/null || true)"
    if [[ -f "${candidate}/LLVMConfig.cmake" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  for candidate in \
    /opt/rocm/llvm/lib/cmake/llvm \
    /opt/rocm/lib/llvm/lib/cmake/llvm; do
    if [[ -f "${candidate}/LLVMConfig.cmake" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  for llvm_config in \
    llvm-config llvm-config-20 llvm-config-19 llvm-config-18 \
    llvm-config-17 llvm-config-16 llvm-config-15; do
    if ! command -v "$llvm_config" >/dev/null 2>&1; then
      continue
    fi
    candidate="$("$llvm_config" --cmakedir 2>/dev/null || true)"
    if [[ -f "${candidate}/LLVMConfig.cmake" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  for candidate in \
    /usr/lib64/cmake/llvm \
    /usr/lib/cmake/llvm \
    /usr/lib64/llvm*/lib/cmake/llvm \
    /usr/lib/llvm*/lib/cmake/llvm; do
    if [[ -f "${candidate}/LLVMConfig.cmake" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  return 1
}

prepare_source_checkout() {
  if [[ ! -e "$UMR_DIR" ]]; then
    if ((SKIP_PULL == 1)); then
      echo "Cannot use --skip-pull; UMR source directory is absent: ${UMR_DIR}" \
        >&2
      return 1
    fi
    echo "Cloning ${UMR_REPOSITORY} branch ${UMR_BRANCH} into ${UMR_DIR}"
    mkdir -p -- "$(dirname -- "$UMR_DIR")" || return
    git clone --branch "$UMR_BRANCH" --single-branch \
      "$UMR_REPOSITORY" "$UMR_DIR" || return
  elif [[ ! -d "$UMR_DIR" ]]; then
    echo "UMR source path is not a directory: ${UMR_DIR}" >&2
    return 1
  fi

  UMR_DIR="$(cd -- "$UMR_DIR" && pwd)" || return
  if ! git -C "$UMR_DIR" rev-parse --is-inside-work-tree \
    >/dev/null 2>&1; then
    echo "Not a Git repository: ${UMR_DIR}" >&2
    return 1
  fi
}

update_source_checkout() {
  if ! git -C "$UMR_DIR" remote get-url "$UMR_REMOTE" >/dev/null 2>&1; then
    echo "UMR remote does not exist: ${UMR_REMOTE}" >&2
    echo "Available remotes:" >&2
    git -C "$UMR_DIR" remote -v >&2
    return 1
  fi

  current_branch="$(git -C "$UMR_DIR" branch --show-current)"
  if [[ "$current_branch" != "$UMR_BRANCH" ]]; then
    if [[ -n "$(git -C "$UMR_DIR" status --porcelain)" ]]; then
      echo "Cannot switch UMR branches with local changes present" >&2
      git -C "$UMR_DIR" status --short >&2
      return 1
    fi

    git -C "$UMR_DIR" fetch "$UMR_REMOTE" "$UMR_BRANCH" || return
    if git -C "$UMR_DIR" show-ref --verify --quiet \
      "refs/heads/${UMR_BRANCH}"; then
      git -C "$UMR_DIR" checkout "$UMR_BRANCH" || return
    else
      git -C "$UMR_DIR" checkout -b "$UMR_BRANCH" \
        --track "$UMR_REMOTE/$UMR_BRANCH" || return
    fi
  fi

  echo "Updating ${UMR_REMOTE}/${UMR_BRANCH} in ${UMR_DIR}"
  git -C "$UMR_DIR" pull --rebase --autostash "$UMR_REMOTE" "$UMR_BRANCH"
}

if ((INSTALL_DEPS == 1)); then
  if ! run_step "common_dependencies_install" run_yum_install \
    gcc gcc-c++ make cmake ncurses-devel libpciaccess-devel zlib-devel; then
    exit 1
  fi
else
  step_skip "common_dependencies_install" "not-requested"
fi

if ! run_step "source_checkout" prepare_source_checkout; then
  exit 1
fi

if ((SKIP_PULL == 0)); then
  if ! run_step "source_update" update_source_checkout; then
    exit 1
  fi
else
  step_skip "source_update" "skip-pull"
fi

UMR_BUILD_DIR="${BUILD_DIR:-${UMR_DIR}/build}"
if [[ "$UMR_BUILD_DIR" != /* ]]; then
  UMR_BUILD_DIR="${UMR_DIR}/${UMR_BUILD_DIR}"
fi

CMAKE_CACHE="${UMR_BUILD_DIR}/CMakeCache.txt"
prepare_build_directory() {
  local cache_build cache_source

  if [[ -f "$CMAKE_CACHE" ]]; then
    cache_source="$(
      awk -F= '/^CMAKE_HOME_DIRECTORY:INTERNAL=/ {
        print substr($0, index($0, "=") + 1)
        exit
      }' "$CMAKE_CACHE"
    )"
    cache_build="$(
      awk -F= '/^CMAKE_CACHEFILE_DIR:INTERNAL=/ {
        print substr($0, index($0, "=") + 1)
        exit
      }' "$CMAKE_CACHE"
    )"
    if [[ "$cache_source" != "$UMR_DIR" ||
          "$cache_build" != "$UMR_BUILD_DIR" ]]; then
      echo "Removing stale CMake build directory after UMR source move:"
      echo "  cached source: ${cache_source:-unknown}"
      echo "  current source: ${UMR_DIR}"
      cmake -E remove_directory "$UMR_BUILD_DIR"
    fi
  fi
}

if ! run_step "build_directory" prepare_build_directory; then
  exit 1
fi

echo "Building UMR commit: $(git -C "$UMR_DIR" rev-parse --short HEAD)"

configure_umr() {
  local llvm_dir="$1"
  local cmake_args=(
    -DCMAKE_BUILD_TYPE=RelWithDebInfo
    -DUMR_NO_DRM=ON
    -DUMR_NO_GUI=ON
    -DUMR_NO_SERVER=ON
  )

  if [[ -n "$llvm_dir" ]]; then
    cmake_args+=(
      -DUMR_NO_LLVM=OFF
      "-DLLVM_DIR=${llvm_dir}"
    )
  else
    cmake_args+=(-DUMR_NO_LLVM=ON)
  fi
  cmake_args+=("${BUILD_EXTRA_ARGS[@]}")

  cmake -S "$UMR_DIR" -B "$UMR_BUILD_DIR" "${cmake_args[@]}"
}

build_umr() {
  cmake --build "$UMR_BUILD_DIR" --parallel "$JOBS"
}

llvm_dir=""
step_begin "llvm_development_detect"
llvm_dir="$(find_llvm_dir || true)"
if [[ -n "$llvm_dir" ]]; then
  log "INFO llvm_cmake_dir=${llvm_dir}"
  step_end "llvm_development_detect" "PASS" 0
else
  echo "LLVM development files were not found"
  step_end "llvm_development_detect" "FAIL" 1
fi

if [[ -z "$llvm_dir" ]]; then
  llvm_package_key="${LLVM_PACKAGE//[^[:alnum:]._-]/_}"
  LLVM_DOWNLOAD_DIR="${LLVM_DOWNLOAD_DIR:-${UMR_DIR}/.umr-deps/${llvm_package_key}}"
  log "INFO llvm_download_dir=${LLVM_DOWNLOAD_DIR}"

  if run_step "llvm_package_search" search_yum_package "$LLVM_PACKAGE"; then
    if run_step "llvm_package_download" download_yum_package \
      "$LLVM_PACKAGE" "$LLVM_DOWNLOAD_DIR"; then
      if run_step "llvm_package_install" install_downloaded_rpms \
        "$LLVM_DOWNLOAD_DIR"; then
        step_begin "llvm_development_redetect"
        llvm_dir="$(find_llvm_dir || true)"
        if [[ -n "$llvm_dir" ]]; then
          log "INFO llvm_cmake_dir=${llvm_dir}"
          step_end "llvm_development_redetect" "PASS" 0
        else
          echo "LLVM package installed but LLVMConfig.cmake is still missing" \
            >&2
          step_end "llvm_development_redetect" "FAIL" 1
        fi
      else
        step_skip "llvm_development_redetect" "package-install-failed"
      fi
    else
      step_skip "llvm_package_install" "package-download-failed"
      step_skip "llvm_development_redetect" "package-download-failed"
    fi
  else
    step_skip "llvm_package_download" "package-search-failed"
    step_skip "llvm_package_install" "package-search-failed"
    step_skip "llvm_development_redetect" "package-search-failed"
  fi
else
  step_skip "llvm_package_search" "development-files-present"
  step_skip "llvm_package_download" "development-files-present"
  step_skip "llvm_package_install" "development-files-present"
  step_skip "llvm_development_redetect" "development-files-present"
fi

llvm_enabled=0
if [[ -n "$llvm_dir" ]]; then
  log "INFO build_mode=llvm llvm_cmake_dir=${llvm_dir}"
  if run_step "cmake_configure_llvm" configure_umr "$llvm_dir"; then
    if run_step "cmake_build_llvm" build_umr; then
      llvm_enabled=1
    else
      echo "LLVM-enabled build failed; retrying without LLVM" >&2
      log "INFO fallback=no-llvm reason=llvm-build-failed"
    fi
  else
    step_skip "cmake_build_llvm" "configure-failed"
    echo "LLVM-enabled configure failed; retrying without LLVM" >&2
    log "INFO fallback=no-llvm reason=llvm-configure-failed"
  fi

  if ((llvm_enabled == 0)); then
    if ! run_step "llvm_build_cleanup" \
      cmake -E remove_directory "$UMR_BUILD_DIR"; then
      exit 1
    fi
  else
    step_skip "llvm_build_cleanup" "llvm-build-passed"
  fi
else
  step_skip "cmake_configure_llvm" "llvm-unavailable"
  step_skip "cmake_build_llvm" "llvm-unavailable"
  step_skip "llvm_build_cleanup" "llvm-unavailable"
  log "INFO fallback=no-llvm reason=llvm-unavailable"
fi

if ((llvm_enabled == 0)); then
  log "INFO build_mode=no-llvm"
  if ! run_step "cmake_configure_no_llvm" configure_umr ""; then
    exit 1
  fi
  if ! run_step "cmake_build_no_llvm" build_umr; then
    exit 1
  fi
else
  step_skip "cmake_configure_no_llvm" "llvm-build-passed"
  step_skip "cmake_build_no_llvm" "llvm-build-passed"
fi

if ((BUILD_PACKAGE == 1)); then
  step_begin "package_commands"
  package_commands_ok=1
  for command in cpack rpmbuild rpm; do
    if ! command -v "$command" >/dev/null 2>&1; then
      echo "Missing package command: ${command}" >&2
      package_commands_ok=0
    fi
  done
  if ((package_commands_ok == 0)); then
    step_end "package_commands" "FAIL" 1
    exit 1
  fi
  step_end "package_commands" "PASS" 0

  if ! run_step "rpm_package_build" bash -c '
    cd "$1"
    cpack -G RPM \
      -D "CPACK_RPM_PACKAGE_REQUIRES=ncurses-libs, libpciaccess, zlib" \
      -D "CPACK_RPM_EXCLUDE_FROM_AUTO_FILELIST_ADDITION=/usr/local;/usr/local/bin;/usr/local/lib64;/usr/local/share;/usr/local/share/man;/usr/local/share/man/man1"
  ' _ "$UMR_BUILD_DIR"; then
    exit 1
  fi

  step_begin "rpm_package_validate"
  shopt -s nullglob
  rpms=("${UMR_BUILD_DIR}"/umr-*.rpm)
  shopt -u nullglob
  if ((${#rpms[@]} == 0)); then
    echo "RPM was not generated under ${UMR_BUILD_DIR}" >&2
    step_end "rpm_package_validate" "FAIL" 1
    exit 1
  fi
  for rpm_path in "${rpms[@]}"; do
    if ! rpm -K "$rpm_path" || ! rpm -qpl "$rpm_path" >/dev/null; then
      step_end "rpm_package_validate" "FAIL" 1
      exit 1
    fi
    echo "Generated RPM: ${rpm_path}"
  done
  step_end "rpm_package_validate" "PASS" 0
else
  step_skip "package_commands" "not-requested"
  step_skip "rpm_package_build" "not-requested"
  step_skip "rpm_package_validate" "not-requested"
fi

UMR_BINARY="${UMR_BUILD_DIR}/src/app/umr"
step_begin "binary_verify"
if [[ ! -x "$UMR_BINARY" ]]; then
  echo "UMR binary was not generated: ${UMR_BINARY}" >&2
  step_end "binary_verify" "FAIL" 1
  exit 1
fi
if ! "$UMR_BINARY" -h >/dev/null; then
  echo "UMR binary help check failed: ${UMR_BINARY}" >&2
  step_end "binary_verify" "FAIL" 1
  exit 1
fi
step_end "binary_verify" "PASS" 0

if ((llvm_enabled == 1)); then
  log "RESULT llvm_disassembly=enabled binary=${UMR_BINARY}"
else
  log "RESULT llvm_disassembly=disabled binary=${UMR_BINARY}"
fi
log "RESULT build_complete binary=${UMR_BINARY}"
