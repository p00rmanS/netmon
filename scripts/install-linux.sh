#!/usr/bin/env bash
# Install NetMon as a service on a Raspberry Pi (or any Linux with systemd),
# so it starts when the box powers on and restarts if it ever crashes.
#
#   Install:    bash scripts/install-linux.sh
#   Uninstall:  bash scripts/install-linux.sh --uninstall
#
# Run it as your normal user from the NetMon folder. It asks for your password
# once (sudo), only to register the service with the system.
set -euo pipefail

SERVICE=netmon
UNIT=/etc/systemd/system/$SERVICE.service
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_AS="$(id -un)"

if [[ "${1:-}" == "--uninstall" ]]; then
  sudo systemctl disable --now "$SERVICE" 2>/dev/null || true
  sudo rm -f "$UNIT"
  sudo systemctl daemon-reload
  echo "NetMon service removed. Your devices, history and settings are still in $DIR."
  exit 0
fi

if [[ "$RUN_AS" == "root" ]]; then
  echo "Please run this as your normal user, not with sudo. It will ask for sudo when it needs it." >&2
  exit 1
fi
command -v systemctl >/dev/null || { echo "This needs systemd (Raspberry Pi OS, Ubuntu, Debian...)." >&2; exit 1; }

PY=python3
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null || {
  echo "NetMon needs Python 3.11 or newer. On Raspberry Pi OS: sudo apt install python3 python3-venv" >&2
  exit 1
}

echo "==> Setting up Python environment in $DIR/.venv"
if [[ ! -x "$DIR/.venv/bin/python" ]]; then
  "$PY" -m venv "$DIR/.venv" || {
    echo "Couldn't create it. Try: sudo apt install python3-venv" >&2
    exit 1
  }
fi
"$DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$DIR/.venv/bin/python" -m pip install --quiet -r "$DIR/requirements.txt"

if [[ ! -f "$DIR/devices.yaml" ]]; then
  cp "$DIR/devices.example.yaml" "$DIR/devices.yaml"
  echo "==> Created devices.yaml from the example; add your devices from the dashboard."
fi

echo "==> Registering the NetMon service (needs your password)"
sudo tee "$UNIT" >/dev/null <<UNIT_EOF
[Unit]
Description=NetMon restaurant network monitor
Wants=network-online.target
After=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m netmon.main --lan
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT_EOF
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo
  echo "NetMon is running and will start automatically when this box powers on."
  echo "Dashboard: http://${IP:-this-box}:8000  (from any phone or computer on the same network)"
  echo "Logs:      $DIR/netmon.log"
else
  echo "NetMon didn't start. See what went wrong with:  journalctl -u $SERVICE -n 30" >&2
  exit 1
fi
