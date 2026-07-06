#!/usr/bin/env bash
# ONE command, whole stack: MT5 terminal -> bridge -> engine -> dashboard.
#
#   ./run.sh            autonomous trading (demo-gated server-side, always)
#   ./run.sh observe    full pipeline, journals every decision, sends NO orders
#   ./run.sh check      onboarding probe only — tells you what's missing
#   ./run.sh gate       backtest every strategy x symbol on YOUR broker's data
#
# The supervisor (executor.run) keeps every part alive: it reboots the bridge
# and terminal if they die and restarts the engine/dashboard with backoff.
# Emergency stop while running: touch intel/executor/data/KILL
set -euo pipefail
cd "$(dirname "$0")/intel"

if ! command -v python3 >/dev/null; then
  echo "python3 not found — install Python 3.10+ first"; exit 1
fi
python3 - <<'EOF' || { echo "numpy missing -> pip install numpy (or apt install python3-numpy)"; exit 1; }
import numpy
EOF

if [ ! -f .env ]; then
  cp .env.example .env
  echo "*** created intel/.env from the example — edit MI_SYMBOLS to YOUR broker's"
  echo "*** symbol names when you get a moment. Safe defaults are active meanwhile."
fi

case "${1:-trade}" in
  check)   exec python3 -m executor.onboard ;;
  gate)    exec python3 -m executor.gate ;;
  observe) MI_EXEC_MODE=observe exec python3 -m executor.run ;;
  vault)   shift; cd ..; exec python3 tools/vault.py "$@" ;;
  trade|*) exec python3 -m executor.run ;;
esac
