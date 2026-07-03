#!/usr/bin/env bash
# ONE command: boots MT5 terminal (Wine) + bridge + engine + dashboard.
#   ./start.sh              trade mode (demo-gated server-side)
#   ./start.sh observe      full pipeline, zero orders
set -euo pipefail
cd "$(dirname "$0")"
if [ "${1:-}" = "observe" ]; then export MI_EXEC_MODE=observe; fi
exec python3 -m executor.run
