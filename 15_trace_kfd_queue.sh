#!/usr/bin/env bash
# Toggle KFD queue/runlist dynamic-debug and follow the kernel log.
set -euo pipefail

# Matched by file, format string, or func name — not line number — so these
# survive kernel rebases. Format rules are not whole-file opens of
# kfd_packet_manager.c (that file also dumps runlist IB hex and floods dmesg).
RULES=(
    'file kfd_device_queue_manager.c'
    'file kfd_process.c'
    'file kfd_mqd_manager_v9.c'
    'format "Building runlist ib"'
    'format "static_queue"'
    'format "Finished map process"'
    'format "runlist IB address"'
    'func restore_process_worker'
    'func evict_process_worker'
)
RULE_FLAGS=( +p +p +p +p +p +p +p +pfl +pfl )

MODULE_PARAMS=(
    debug_evictions
    halt_if_hws_hang
)

DD=/proc/dynamic_debug/control
[[ -e $DD ]] || DD=/sys/kernel/debug/dynamic_debug/control
MOD=/sys/module/amdgpu/parameters

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat << USAGE
Usage: ${0##*/} {enable|disable|status|capture [LOG]}

enable   Turn on the KFD debug rules above + module params (debug_evictions,
         halt_if_hws_hang).
disable  Turn them off (module params back to default 0).
status   Show module params and how many callsites are enabled per file.
capture  enable, then tee the kernel journal into LOG until Ctrl-C.
         Debug stays enabled afterwards; run "disable" explicitly.
         Default LOG: ./kfd-queue-<timestamp>.log
USAGE
}

files() {
    local rule
    for rule in "${RULES[@]}"; do
        [[ $rule == file\ * ]] && printf '%s\n' "${rule#file }"
    done | sort -u
}

apply() {
    local enable=$1 rule flag i param path
    [[ -e $DD ]] || die "dynamic debug control not found (CONFIG_DYNAMIC_DEBUG?)"
    for param in "${MODULE_PARAMS[@]}"; do
        [[ -e $MOD/$param ]] || die "not found: $MOD/$param"
    done
    for i in "${!RULES[@]}"; do
        rule=${RULES[$i]}
        if [[ $enable == 1 ]]; then
            flag=${RULE_FLAGS[$i]}
        else
            flag=${RULE_FLAGS[$i]/+/-}
        fi
        printf '%s %s\n' "$rule" "$flag"
    done | sudo tee "$DD" > /dev/null
    for param in "${MODULE_PARAMS[@]}"; do
        printf '%d\n' "$enable" | sudo tee "$MOD/$param" > /dev/null
    done
    printf 'KFD dynamic debug %s' \
        "$([[ $enable == 1 ]] && echo enabled || echo disabled)"
    for param in "${MODULE_PARAMS[@]}"; do
        printf ', %s=%d' "$param" "$enable"
    done
    printf '\n'
}

status() {
    local f param
    for param in "${MODULE_PARAMS[@]}"; do
        printf '%s: %s\n' "$param" \
            "$(sudo cat "$MOD/$param" 2> /dev/null || echo '?')"
    done
    for f in $(files); do
        sudo awk -v file="$f" '
            $1 ~ ("/" file ":") {
                for (i = 2; i <= NF; i++)
                    if ($i ~ /^=/) { total++; on += ($i ~ /p/); break }
            }
            END { printf "  %-32s %d/%d callsites on\n", file, on, total }
        ' "$DD"
    done
}

case ${1-capture} in
    enable)  apply 1 ;;
    disable) apply 0 ;;
    status)  status ;;
    -h | --help) usage ;;
    capture | *)
        [[ ${1-} == capture ]] && shift
        log=${1:-kfd-queue-$(date +%Y%m%d-%H%M%S).log}
        apply 1
        printf 'Logging to %s (Ctrl-C stops capture; debug stays on)\n' "$log"
        sudo journalctl -k -f -n0 -o short-monotonic --no-pager |
            stdbuf -oL tee "$log"
        ;;
esac
