#!/usr/bin/env bash
# install.sh — install market-intel-executor as a persistent systemd user service.
#
# Runs the executor stack (MT5 bridge + engine + dashboard) as the current user,
# surviving reboots and restarts. One command on a fresh machine:
#
#   bash intel/ops/install.sh
#
# After install, manage with:
#   systemctl --user status  market-intel-executor
#   systemctl --user restart market-intel-executor
#   systemctl --user stop    market-intel-executor
#   journalctl --user -u market-intel-executor -f   (live logs)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTEL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_DIR="$HOME/.config/systemd/user"
SERVICE_NAME="market-intel-executor"

echo "=== market-intel executor installer ==="
echo "Intel dir : $INTEL_DIR"
echo "Service   : $SERVICE_DIR/$SERVICE_NAME.service"
echo ""

# ---- pre-flight checks --------------------------------------------------------
command -v python3 >/dev/null || { echo "ERROR: python3 not found"; exit 1; }
python3 -c "import sys; assert sys.version_info >= (3,10), 'Python 3.10+ required'" \
  || { echo "ERROR: Python 3.10+ required"; exit 1; }
python3 -c "import numpy" 2>/dev/null \
  || { echo "ERROR: numpy not installed — run: pip3 install numpy"; exit 1; }

if [ ! -f "$INTEL_DIR/.env" ]; then
  if [ -f "$INTEL_DIR/.env.example" ]; then
    cp "$INTEL_DIR/.env.example" "$INTEL_DIR/.env"
    echo "NOTICE: Created $INTEL_DIR/.env from .env.example"
    echo "        Edit it before starting (at minimum, check MI_SYMBOLS)."
  else
    echo "WARNING: No .env found — using built-in defaults from config.py"
  fi
fi

# ---- write the service file with the real path --------------------------------
mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_DIR/$SERVICE_NAME.service" <<EOF
[Unit]
Description=market-intel executor (MT5 bridge + engine + dashboard)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INTEL_DIR
ExecStart=/usr/bin/python3 -m executor.run
Restart=always
RestartSec=20
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

# ---- enable and start ---------------------------------------------------------
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user start  "$SERVICE_NAME"

# Make the service survive logout (keeps running after SSH session ends)
loginctl enable-linger "$USER" 2>/dev/null && echo "Linger enabled (service survives logout)"

echo ""
echo "=== DONE ==="
echo "Service is running. Dashboard: http://127.0.0.1:8877"
echo ""
echo "Useful commands:"
echo "  systemctl --user status  $SERVICE_NAME"
echo "  systemctl --user restart $SERVICE_NAME"
echo "  journalctl --user -u $SERVICE_NAME -f"
echo "  tail -f $INTEL_DIR/../intel/logs/engine.log"
echo ""
echo "To stop ALL trading immediately:"
echo "  touch $INTEL_DIR/executor/data/KILL"
echo "  (remove the file to resume)"
