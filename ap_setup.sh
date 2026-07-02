#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
DEFAULT_CONNECTION_NAME="BirdNET-Display-AP"

MODE=""
YES=0
CONFIG_FILE=""
AP_SSID="${AP_SSID:-}"
AP_PASSWORD="${AP_PASSWORD:-}"
WIFI_INTERFACE="${WIFI_INTERFACE:-}"
CONNECTION_NAME="${CONNECTION_NAME:-$DEFAULT_CONNECTION_NAME}"
BACKUP_DIR="${BACKUP_DIR:-./ap_setup_backups}"
ARGS=("$@")

usage() {
  cat <<EOF
Usage:
  $SCRIPT_NAME --dry-run --ssid <ssid> --password <passphrase> --interface <wifi-iface> [options]
  sudo $SCRIPT_NAME --apply --ssid <ssid> --password <passphrase> --interface <wifi-iface> [options]
  sudo $SCRIPT_NAME --apply --config ap_setup.conf [--yes]

Options:
  --dry-run                 Print what would change; do not modify NetworkManager.
  --apply                   Apply the AP configuration.
  --yes                     Skip the apply confirmation prompt.
  --config <file>           Load AP_SSID, AP_PASSWORD, WIFI_INTERFACE, and optional CONNECTION_NAME.
  --ssid <ssid>             Access point SSID. Required.
  --password <passphrase>   WPA/WPA2 passphrase, 8-63 characters. Required.
  --interface <iface>       Wireless interface to use, such as wlan1. Required.
  --connection-name <name>  NetworkManager connection name. Default: $DEFAULT_CONNECTION_NAME
  --help                    Show this help.

No changes are made unless --apply is provided. Use --dry-run first.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

mask_password() {
  if [[ -z "$AP_PASSWORD" ]]; then
    echo "(missing)"
  else
    echo "********"
  fi
}

load_config() {
  local file="$1"
  [[ -f "$file" ]] || die "Config file not found: $file"

  # shellcheck disable=SC1090
  source "$file"

  AP_SSID="${AP_SSID:-}"
  AP_PASSWORD="${AP_PASSWORD:-}"
  WIFI_INTERFACE="${WIFI_INTERFACE:-}"
  CONNECTION_NAME="${CONNECTION_NAME:-$DEFAULT_CONNECTION_NAME}"
}

# Load config first so explicit command-line flags can override it.
idx=0
while [[ $idx -lt ${#ARGS[@]} ]]; do
  if [[ "${ARGS[$idx]}" == "--config" ]]; then
    next=$((idx + 1))
    [[ $next -lt ${#ARGS[@]} ]] || die "--config requires a file path."
    CONFIG_FILE="${ARGS[$next]}"
    load_config "$CONFIG_FILE"
    break
  fi
  idx=$((idx + 1))
done

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      [[ -z "$MODE" ]] || die "Use only one of --dry-run or --apply."
      MODE="dry-run"
      shift
      ;;
    --apply)
      [[ -z "$MODE" ]] || die "Use only one of --dry-run or --apply."
      MODE="apply"
      shift
      ;;
    --yes|--non-interactive)
      YES=1
      shift
      ;;
    --config)
      [[ $# -ge 2 ]] || die "--config requires a file path."
      CONFIG_FILE="$2"
      shift 2
      ;;
    --ssid)
      [[ $# -ge 2 ]] || die "--ssid requires a value."
      AP_SSID="$2"
      shift 2
      ;;
    --password)
      [[ $# -ge 2 ]] || die "--password requires a value."
      AP_PASSWORD="$2"
      shift 2
      ;;
    --interface)
      [[ $# -ge 2 ]] || die "--interface requires a value."
      WIFI_INTERFACE="$2"
      shift 2
      ;;
    --connection-name)
      [[ $# -ge 2 ]] || die "--connection-name requires a value."
      CONNECTION_NAME="$2"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  usage
  exit 2
fi

[[ -n "$AP_SSID" ]] || die "Missing AP SSID. Provide --ssid or AP_SSID in a config file."
[[ -n "$AP_PASSWORD" ]] || die "Missing AP password. Provide --password or AP_PASSWORD in a config file."
[[ "$AP_SSID" != *$'\n'* && "$AP_SSID" != *$'\r'* ]] || die "SSID must not contain line breaks."
[[ "$AP_PASSWORD" != *$'\n'* && "$AP_PASSWORD" != *$'\r'* ]] || die "AP password must not contain line breaks."
[[ ${#AP_PASSWORD} -ge 8 ]] || die "AP password must be at least 8 characters."
[[ ${#AP_PASSWORD} -le 63 ]] || die "AP password must be no more than 63 characters."
[[ -n "$WIFI_INTERFACE" ]] || die "Missing Wi-Fi interface. Provide --interface or WIFI_INTERFACE in a config file."
[[ -n "$CONNECTION_NAME" ]] || die "Connection name must not be empty."
[[ "$WIFI_INTERFACE" =~ ^[A-Za-z0-9_.:-]+$ ]] || die "Invalid Wi-Fi interface name: $WIFI_INTERFACE"
[[ "$CONNECTION_NAME" != *$'\n'* && "$CONNECTION_NAME" != *$'\r'* ]] || die "Connection name must not contain line breaks."

command -v nmcli >/dev/null 2>&1 || die "Missing dependency: nmcli."

[[ -e "/sys/class/net/$WIFI_INTERFACE" ]] || die "Network interface does not exist: $WIFI_INTERFACE"
if [[ ! -d "/sys/class/net/$WIFI_INTERFACE/wireless" ]]; then
  if command -v iw >/dev/null 2>&1; then
    iw dev "$WIFI_INTERFACE" info >/dev/null 2>&1 || die "Interface does not appear to be wireless-capable: $WIFI_INTERFACE"
  else
    die "Interface does not appear wireless-capable and 'iw' is unavailable for verification: $WIFI_INTERFACE"
  fi
fi

connection_exists=0
if nmcli -t -f NAME connection show | grep -Fxq "$CONNECTION_NAME"; then
  connection_exists=1
fi

print_summary() {
  cat <<EOF
BirdNET Display AP setup summary
--------------------------------
Mode:                 $MODE
Wi-Fi interface:      $WIFI_INTERFACE
AP SSID:              $AP_SSID
AP password:          $(mask_password)
Connection name:      $CONNECTION_NAME
MAC override:         none
Target NM connection: $CONNECTION_NAME
Existing connection:  $([[ "$connection_exists" -eq 1 ]] && echo "will be updated" || echo "will be created")

NetworkManager changes:
  - Create or update only the '$CONNECTION_NAME' connection.
  - Configure it as a WPA-PSK Wi-Fi AP on '$WIFI_INTERFACE'.
  - Use NetworkManager shared IPv4 for AP clients.
  - Do not delete unrelated connections.
  - Do not edit NetworkManager.conf or dnsmasq shared config.
  - Do not restart NetworkManager.
EOF
}

print_summary

if [[ "$MODE" == "dry-run" ]]; then
  echo
  echo "Dry run only. No NetworkManager changes were made."
  exit 0
fi

if [[ "$EUID" -ne 0 ]]; then
  die "Apply mode must be run as root. Re-run with sudo."
fi

if [[ "$YES" -ne 1 ]]; then
  echo
  read -r -p "Apply this NetworkManager AP configuration? Type 'yes' to continue: " answer
  [[ "$answer" == "yes" ]] || die "Aborted without making changes."
fi

if [[ "$connection_exists" -eq 1 ]]; then
  mkdir -p "$BACKUP_DIR"
  backup_file="$BACKUP_DIR/${CONNECTION_NAME//[^A-Za-z0-9_.-]/_}.$(date +%Y%m%d-%H%M%S).nmcli.txt"
  nmcli --show-secrets connection show "$CONNECTION_NAME" > "$backup_file"
  echo "Saved existing connection snapshot: $backup_file"
else
  nmcli connection add type wifi ifname "$WIFI_INTERFACE" con-name "$CONNECTION_NAME" autoconnect yes ssid "$AP_SSID"
fi

nmcli connection modify "$CONNECTION_NAME" \
  connection.interface-name "$WIFI_INTERFACE" \
  802-11-wireless.ssid "$AP_SSID" \
  802-11-wireless.mode ap \
  802-11-wireless.band bg \
  wifi.powersave 1 \
  wifi-sec.key-mgmt wpa-psk \
  wifi-sec.proto rsn \
  wifi-sec.pairwise ccmp \
  wifi-sec.group ccmp \
  wifi-sec.psk "$AP_PASSWORD" \
  ipv4.method shared \
  ipv6.method ignore

nmcli connection up "$CONNECTION_NAME"

cat <<EOF

AP connection is configured.

Inspect:
  nmcli connection show "$CONNECTION_NAME"

Remove:
  sudo nmcli connection delete "$CONNECTION_NAME"

Unrelated NetworkManager connections were not deleted or modified by this script.
EOF
