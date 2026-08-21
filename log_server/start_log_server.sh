#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: start_log_server.sh LOG_DIRECTORY [--port PORT] [--bind ADDRESS]

Serve LOG_DIRECTORY with the GPU debug bundle browser.
Defaults: --port 30000 --bind 0.0.0.0
Options may appear before or after LOG_DIRECTORY.
EOF
}

port=30000
bind_address=0.0.0.0
log_directory=

while (($#)); do
    case "$1" in
        --port)
            (($# >= 2)) || { echo "error: --port requires a value" >&2; exit 2; }
            port=$2
            shift 2
            ;;
        --port=*)
            port=${1#*=}
            shift
            ;;
        --bind)
            (($# >= 2)) || { echo "error: --bind requires a value" >&2; exit 2; }
            bind_address=$2
            shift 2
            ;;
        --bind=*)
            bind_address=${1#*=}
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [[ -n "$log_directory" ]]; then
                echo "error: provide exactly one log directory" >&2
                usage >&2
                exit 2
            fi
            log_directory=$1
            shift
            ;;
    esac
done

if [[ -z "$log_directory" ]]; then
    echo "error: LOG_DIRECTORY is required" >&2
    usage >&2
    exit 2
fi
if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    echo "error: port must be an integer from 1 through 65535" >&2
    exit 2
fi
if [[ -z "$bind_address" ]]; then
    echo "error: bind address must not be empty" >&2
    exit 2
fi
if [[ ! -d "$log_directory" ]]; then
    echo "error: log directory does not exist or is not a directory: $log_directory" >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 is required" >&2
    exit 127
fi

script_directory=$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd -P
)
server_script="$script_directory/server.py"
if [[ ! -f "$server_script" ]]; then
    echo "error: co-located server.py was not found" >&2
    exit 2
fi

resolved_root=$(
    cd -- "$log_directory"
    pwd -P
)

printf 'Serving root: %s\n' "$resolved_root"
printf 'Listening URL: http://%s:%s/\n' "$bind_address" "$port"
exec python3 "$server_script" "$resolved_root" --port "$port" --bind "$bind_address"
