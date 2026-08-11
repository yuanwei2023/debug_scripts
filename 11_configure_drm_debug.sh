#!/usr/bin/env bash
# Author: Perry_Yuan@outlook.com
# Configure DRM debug categories and dynamic-debug source files selectively.

set -Eeuo pipefail

DRM_DEBUG_PARAM="${DRM_DEBUG_PARAM:-/sys/module/drm/parameters/debug}"
DYNDBG_CTL="${DYNDBG_CTL:-/sys/kernel/debug/dynamic_debug/control}"
DRM_DEBUG_NO_SUDO="${DRM_DEBUG_NO_SUDO:-0}"

ACTION=""
ACTION_EXPLICIT=0
DRM_CATEGORIES=""
RAW_MASK=""
DRY_RUN=0
FILES=()

readonly ALL_DRM_MASK=0x3ff
readonly -a DRM_CATEGORY_ORDER=(
  core driver kms prime atomic vbl state lease dp drmres
)
declare -Ar DRM_CATEGORY_BITS=(
  [core]=0x001
  [driver]=0x002
  [kms]=0x004
  [prime]=0x008
  [atomic]=0x010
  [vbl]=0x020
  [state]=0x040
  [lease]=0x080
  [dp]=0x100
  [drmres]=0x200
)

usage() {
  cat <<'USAGE'
Usage:
  11_configure_drm_debug.sh [enable|disable|set|show] [options]

Selectively configure the DRM debug mask and Linux dynamic-debug callsites.
Without an action, selectors default to "enable"; no selectors default to "show".

Actions:
  enable                    Add categories and enable selected files
  disable                   Remove categories and disable selected files
  set                       Replace drm.debug; enable selected files
  show                      Show current drm.debug and matching file callsites

Options:
      --drm LIST            Comma-separated DRM categories
                            core,driver,kms,prime,atomic,vbl,state,lease,dp,drmres
                            Special values: all, none
      --mask MASK           Raw decimal or hexadecimal DRM debug mask
  -f, --file PATTERN        Dynamic-debug source file/path pattern (repeatable)
                            Quote wildcards, for example 'amdgpu_*.c'
  -n, --dry-run             Print writes without changing kernel state
      --list-categories     Print category bits and exit
  -h, --help                Show this help

Examples:
  ./11_configure_drm_debug.sh enable --drm driver,kms \
    --file amdgpu_job.c --file kfd_chardev.c
  ./11_configure_drm_debug.sh enable --mask 0x6 \
    --file 'drivers/gpu/drm/amd/amdgpu/*.c'
  ./11_configure_drm_debug.sh disable --drm kms --file amdgpu_job.c
  ./11_configure_drm_debug.sh set --drm none
  ./11_configure_drm_debug.sh show --file kfd_chardev.c

Environment overrides:
  DRM_DEBUG_PARAM, DYNDBG_CTL, DRM_DEBUG_NO_SUDO

Changes remain active until explicitly disabled, changed, or the system reboots.
USAGE
}

die() {
  echo "[drm-debug] ERROR: $*" >&2
  exit 1
}

warn() {
  echo "[drm-debug] WARNING: $*" >&2
}

set_action() {
  local requested="$1"
  if (( ACTION_EXPLICIT == 1 )) && [[ "$ACTION" != "$requested" ]]; then
    die "only one action may be specified"
  fi
  ACTION="$requested"
  ACTION_EXPLICIT=1
}

list_categories() {
  local name
  for name in "${DRM_CATEGORY_ORDER[@]}"; do
    printf '%-8s 0x%03x\n' "$name" "$(( DRM_CATEGORY_BITS[$name] ))"
  done
  printf '%-8s 0x%03x\n' "all" "$ALL_DRM_MASK"
}

validate_file_pattern() {
  local pattern="$1"
  [[ -n "$pattern" ]] || die "--file requires a non-empty pattern"
  if [[ ! "$pattern" =~ ^[[:alnum:]_./*?+-]+$ ]]; then
    die "unsafe dynamic-debug file pattern: ${pattern}"
  fi
}

parse_numeric_mask() {
  local value="$1"
  if [[ ! "$value" =~ ^(0[xX][0-9a-fA-F]+|[0-9]+)$ ]]; then
    die "invalid DRM debug mask: ${value}"
  fi
  if [[ "$value" == 0[xX]* ]]; then
    printf '%u\n' "$(( value ))"
  else
    printf '%u\n' "$(( 10#$value ))"
  fi
}

parse_category_mask() {
  local spec="$1"
  local name
  local mask=0
  local saw_none=0
  local -a names=()

  IFS=',' read -r -a names <<<"$spec"
  ((${#names[@]} > 0)) || die "--drm requires at least one category"

  for name in "${names[@]}"; do
    name="${name,,}"
    [[ -n "$name" ]] || die "empty DRM category in: ${spec}"
    case "$name" in
      all)
        mask=$(( mask | ALL_DRM_MASK ))
        ;;
      none)
        saw_none=1
        ;;
      *)
        [[ -v "DRM_CATEGORY_BITS[$name]" ]] ||
          die "unknown DRM category '${name}'; use --list-categories"
        mask=$(( mask | DRM_CATEGORY_BITS[$name] ))
        ;;
    esac
  done

  if (( saw_none == 1 && mask != 0 )); then
    die "'none' cannot be combined with other DRM categories"
  fi
  printf '%u\n' "$mask"
}

while (($# > 0)); do
  case "$1" in
    enable|disable|set|show)
      set_action "$1"
      shift
      ;;
    status)
      set_action "show"
      shift
      ;;
    --drm)
      (($# >= 2)) || die "$1 requires a value"
      [[ -z "$DRM_CATEGORIES" ]] || die "--drm may only be specified once"
      DRM_CATEGORIES="$2"
      shift 2
      ;;
    --mask)
      (($# >= 2)) || die "$1 requires a value"
      [[ -z "$RAW_MASK" ]] || die "--mask may only be specified once"
      RAW_MASK="$2"
      shift 2
      ;;
    -f|--file)
      (($# >= 2)) || die "$1 requires a value"
      validate_file_pattern "$2"
      FILES+=("$2")
      shift 2
      ;;
    -n|--dry-run)
      DRY_RUN=1
      shift
      ;;
    --list-categories)
      list_categories
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

[[ -z "$DRM_CATEGORIES" || -z "$RAW_MASK" ]] ||
  die "--drm and --mask are mutually exclusive"

if [[ -z "$ACTION" ]]; then
  if [[ -n "$DRM_CATEGORIES" || -n "$RAW_MASK" || ${#FILES[@]} -gt 0 ]]; then
    ACTION="enable"
  else
    ACTION="show"
  fi
fi

if [[ "$ACTION" == "set" && -z "$DRM_CATEGORIES" && -z "$RAW_MASK" ]]; then
  die "set requires --drm or --mask"
fi

if [[ "$ACTION" != "show" && -z "$DRM_CATEGORIES" && -z "$RAW_MASK" &&
      ${#FILES[@]} -eq 0 ]]; then
  die "${ACTION} requires --drm, --mask, or --file"
fi

if (( EUID == 0 )) || [[ "$DRM_DEBUG_NO_SUDO" == "1" ]]; then
  ROOT_CMD=()
else
  command -v sudo >/dev/null 2>&1 ||
    die "root access is required and sudo is unavailable"
  ROOT_CMD=(sudo)
fi

run_root() {
  "${ROOT_CMD[@]}" "$@"
}

write_root() {
  local path="$1"
  local value="$2"

  if (( DRY_RUN == 1 )); then
    printf '[drm-debug] dry-run: write %q to %q\n' "$value" "$path"
    return 0
  fi
  printf '%s\n' "$value" | run_root tee "$path" >/dev/null
}

read_drm_mask() {
  local value
  value="$(run_root awk 'NR == 1 { print; exit }' "$DRM_DEBUG_PARAM")"
  parse_numeric_mask "$value"
}

format_mask_categories() {
  local mask="$1"
  local name
  local -a enabled=()

  for name in "${DRM_CATEGORY_ORDER[@]}"; do
    if (( mask & DRM_CATEGORY_BITS[$name] )); then
      enabled+=("$name")
    fi
  done

  if ((${#enabled[@]} == 0)); then
    printf 'none'
  else
    local IFS=,
    printf '%s' "${enabled[*]}"
  fi

  if (( mask & ~ALL_DRM_MASK )); then
    printf ',unknown=0x%x' "$(( mask & ~ALL_DRM_MASK ))"
  fi
}

show_drm_state() {
  local mask
  if ! run_root test -r "$DRM_DEBUG_PARAM"; then
    warn "DRM debug parameter is unavailable: ${DRM_DEBUG_PARAM}"
    return 0
  fi

  mask="$(read_drm_mask)"
  printf 'drm.debug: 0x%x (%s)\n' "$mask" "$(format_mask_categories "$mask")"
}

line_matches_file_patterns() {
  local source_file="$1"
  local base_name="${source_file##*/}"
  local pattern

  for pattern in "${FILES[@]}"; do
    if [[ "$source_file" == $pattern || "$base_name" == $pattern ]]; then
      return 0
    fi
  done
  return 1
}

show_dynamic_debug_state() {
  local line source_file
  local matches=0

  ((${#FILES[@]} > 0)) || return 0
  if ! run_root test -r "$DYNDBG_CTL"; then
    warn "dynamic-debug control is unavailable: ${DYNDBG_CTL}"
    return 0
  fi

  printf 'dynamic-debug matches:\n'
  while IFS= read -r line; do
    source_file="${line%%:*}"
    if line_matches_file_patterns "$source_file"; then
      printf '  %s\n' "$line"
      matches=$(( matches + 1 ))
    fi
  done < <(run_root awk '{ print }' "$DYNDBG_CTL")

  (( matches > 0 )) ||
    printf '  (no callsites matched the requested file patterns)\n'
}

apply_drm_mask() {
  local selected_mask current_mask new_mask

  if [[ -z "$DRM_CATEGORIES" && -z "$RAW_MASK" ]]; then
    return 0
  fi
  run_root test -e "$DRM_DEBUG_PARAM" ||
    die "DRM debug parameter is unavailable: ${DRM_DEBUG_PARAM}"

  if [[ -n "$RAW_MASK" ]]; then
    selected_mask="$(parse_numeric_mask "$RAW_MASK")"
  else
    selected_mask="$(parse_category_mask "$DRM_CATEGORIES")"
  fi

  current_mask="$(read_drm_mask)"
  case "$ACTION" in
    enable)
      new_mask=$(( current_mask | selected_mask ))
      ;;
    disable)
      new_mask=$(( current_mask & ~selected_mask ))
      ;;
    set)
      new_mask="$selected_mask"
      ;;
    *)
      return 0
      ;;
  esac

  printf -v new_mask '0x%x' "$new_mask"
  write_root "$DRM_DEBUG_PARAM" "$new_mask"
}

apply_dynamic_debug_files() {
  local pattern command_suffix

  ((${#FILES[@]} > 0)) || return 0
  run_root test -e "$DYNDBG_CTL" ||
    die "dynamic-debug control is unavailable: ${DYNDBG_CTL}; mount debugfs and enable CONFIG_DYNAMIC_DEBUG"

  case "$ACTION" in
    enable|set)
      command_suffix="+p"
      ;;
    disable)
      command_suffix="-p"
      ;;
    *)
      return 0
      ;;
  esac

  for pattern in "${FILES[@]}"; do
    write_root "$DYNDBG_CTL" "file ${pattern} ${command_suffix}"
  done
}

if [[ "$ACTION" != "show" ]]; then
  apply_drm_mask
  apply_dynamic_debug_files
fi

show_drm_state
show_dynamic_debug_state
