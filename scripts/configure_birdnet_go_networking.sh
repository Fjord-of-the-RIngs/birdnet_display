#!/bin/bash
set -euo pipefail

SERVICE_NAME="${BIRDNET_GO_SERVICE:-birdnet-go}"
MODE=""
BACKUP_ROOT="/var/backups/birdnet-display/birdnet-go-networking"
DROPIN_NAME="birdnet-display-networking.conf"

usage() {
    cat <<EOF
Optional advanced BirdNET-Go host-networking setup.

This script only manages a BirdNET Display-owned systemd drop-in. It does not
rewrite the main BirdNET-Go service and does not remove unrelated drop-ins.

Usage:
  $0 --dry-run [--service birdnet-go]
  $0 --apply   [--service birdnet-go]

Options:
  --dry-run       Print the planned change. Do not write files or restart services.
  --apply         Write the drop-in, reload systemd, and restart the service.
  --service NAME  Systemd service name without ".service" suffix.
  -h, --help      Show this help.

Environment:
  BIRDNET_GO_SERVICE  Default service name when --service is not provided.
EOF
}

die() {
    echo "[ERROR] $*" >&2
    exit 1
}

info() {
    echo "[INFO] $*"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run)
            [ -z "$MODE" ] || die "Choose only one mode: --dry-run or --apply."
            MODE="dry-run"
            shift
            ;;
        --apply)
            [ -z "$MODE" ] || die "Choose only one mode: --dry-run or --apply."
            MODE="apply"
            shift
            ;;
        --service)
            [ "$#" -ge 2 ] || die "--service requires a value."
            SERVICE_NAME="${2%.service}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            die "Unknown argument: $1"
            ;;
    esac
done

if [ -z "$MODE" ]; then
    usage
    exit 0
fi

UNIT="${SERVICE_NAME}.service"
DROPIN_DIR="/etc/systemd/system/${UNIT}.d"
DROPIN_PATH="${DROPIN_DIR}/${DROPIN_NAME}"
BACKUP_DIR="${BACKUP_ROOT}/$(date +%Y%m%d-%H%M%S)"

require_root_for_apply() {
    if [ "$MODE" = "apply" ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
        die "Run with sudo for --apply. Dry-run does not require root."
    fi
}

unit_exists() {
    systemctl cat "$UNIT" >/dev/null 2>&1
}

extract_execstart() {
    systemctl cat "$UNIT" | awk '
        /^[[:space:]]*#/ { next }
        in_exec {
            line=$0
            sub(/^[[:space:]]+/, "", line)
            continued = line ~ /\\[[:space:]]*$/
            sub(/[[:space:]]*\\[[:space:]]*$/, "", line)
            exec = exec " " line
            if (!continued) {
                print exec
                exit
            }
            next
        }
        /^[[:space:]]*ExecStart=/ {
            line=$0
            sub(/^[[:space:]]*ExecStart=/, "", line)
            if (line == "") {
                next
            }
            continued = line ~ /\\[[:space:]]*$/
            sub(/[[:space:]]*\\[[:space:]]*$/, "", line)
            exec = line
            if (!continued) {
                print exec
                exit
            }
            in_exec = 1
        }
    '
}

build_dropin_content() {
    local exec_start="$1"
    local new_exec_start="$2"

    cat <<EOF
# Managed by BirdNET Display.
# Created by scripts/configure_birdnet_go_networking.sh.
# Remove this file and run "systemctl daemon-reload" to stop using this override.
[Service]
ExecStart=
ExecStart=${new_exec_start}
EOF
}

insert_host_network() {
    local exec_start="$1"

    if [[ "$exec_start" =~ (^|[[:space:]])--(network|net)(=|[[:space:]]+)host($|[[:space:]]) ]]; then
        echo ""
        return 2
    fi

    if [[ "$exec_start" =~ (^|[[:space:]])--(network|net)(=|[[:space:]])[^[:space:]]+ ]]; then
        die "Existing non-host Docker network option found. Refusing to overwrite user networking: ${exec_start}"
    fi

    if [[ ! "$exec_start" =~ (^|[[:space:]])([^[:space:]]*/)?docker[[:space:]]+run($|[[:space:]]) ]]; then
        die "Could not find a recognizable 'docker run' ExecStart. Refusing to generate a drop-in."
    fi

    echo "$exec_start" | sed -E 's#(^|[[:space:]])(([^[:space:]]*/)?docker[[:space:]]+run)([[:space:]]+|$)#\1\2 --network host #'
}

require_root_for_apply

if ! unit_exists; then
    die "Systemd service '${UNIT}' was not found. No changes made."
fi

EXEC_START="$(extract_execstart)"
[ -n "$EXEC_START" ] || die "No usable ExecStart found in '${UNIT}'. No changes made."

set +e
NEW_EXEC_START="$(insert_host_network "$EXEC_START")"
INSERT_STATUS=$?
set -e

if [ "$INSERT_STATUS" -eq 2 ]; then
    info "'${UNIT}' already appears to use Docker host networking. No changes needed."
    exit 0
elif [ "$INSERT_STATUS" -ne 0 ]; then
    exit "$INSERT_STATUS"
fi

DROPIN_CONTENT="$(build_dropin_content "$EXEC_START" "$NEW_EXEC_START")"

info "Service: ${UNIT}"
info "Drop-in path: ${DROPIN_PATH}"
info "Backup directory: ${BACKUP_DIR}"
if [ -d "$DROPIN_DIR" ]; then
    info "Drop-in directory exists and will be preserved: ${DROPIN_DIR}"
else
    info "Drop-in directory would be created: ${DROPIN_DIR}"
fi
if [ -f "$DROPIN_PATH" ]; then
    info "Existing BirdNET Display-owned drop-in would be changed and backed up: ${DROPIN_PATH}"
else
    info "BirdNET Display-owned drop-in would be created: ${DROPIN_PATH}"
fi

echo
echo "Generated drop-in content:"
echo "-----"
printf "%s\n" "$DROPIN_CONTENT"
echo "-----"
echo

if [ "$MODE" = "dry-run" ]; then
    info "Dry run only. No files written, systemd not reloaded, service not restarted."
    exit 0
fi

mkdir -p "$DROPIN_DIR"
mkdir -p "$BACKUP_DIR"

if [ -f "$DROPIN_PATH" ]; then
    cp "$DROPIN_PATH" "$BACKUP_DIR/$DROPIN_NAME"
    info "Backed up existing drop-in to: $BACKUP_DIR/$DROPIN_NAME"
fi

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
printf "%s\n" "$DROPIN_CONTENT" > "$tmp_file"
install -m 0644 "$tmp_file" "$DROPIN_PATH"

info "Wrote drop-in: $DROPIN_PATH"
info "Reloading systemd..."
systemctl daemon-reload
info "Restarting ${UNIT}..."
systemctl restart "$UNIT"
info "Done. Inspect with: systemctl cat ${SERVICE_NAME}"
