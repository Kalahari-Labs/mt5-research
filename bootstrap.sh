#!/usr/bin/env bash
# bootstrap.sh — fresh machine → verified, demo-gated workspace in ONE command:
#
#   curl -fsSL https://raw.githubusercontent.com/Kalahari-Labs/mt5-research/main/bootstrap.sh | bash
#
# What it does (and refuses to do):
#   1. clones the repo (or updates the checkout it is run from)
#   2. preflights python3>=3.10 + numpy (the ONLY runtime dependency)
#   3. runs the full unit-test suite — zero installs, stdlib unittest
#   4. seeds intel/.env from the example if missing
#   5. offers the encrypted credential vault (tools/vault.py, openssl-backed)
#   6. prints the next steps for YOUR platform (Wine bridge / native Windows)
#
# It will NOT: enable live trading (structurally impossible without the triple
# gate), install a systemd service without being asked (pass --service), or
# write anywhere outside the repo dir and ~/.config/mt5-research.
set -euo pipefail

REPO_URL="https://github.com/Kalahari-Labs/mt5-research.git"
DIR="${MI_DIR:-$HOME/mt5-research}"
WANT_SERVICE=0
[ "${1:-}" = "--service" ] && WANT_SERVICE=1

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

# --- locate or fetch the repo ---------------------------------------------------
if [ -f "./registry.py" ] && [ -d "./intel" ]; then
  DIR="$(pwd)"
elif [ ! -f "$DIR/registry.py" ]; then
  say "cloning $REPO_URL -> $DIR"
  command -v git >/dev/null || { echo "ERROR: git required"; exit 1; }
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"

say "preflight"
command -v python3 >/dev/null || { echo "ERROR: python3 not found (need 3.10+)"; exit 1; }
python3 - <<'PY'
import sys
assert sys.version_info >= (3, 10), "Python 3.10+ required, found %s" % sys.version
print("python %d.%d.%d OK" % sys.version_info[:3])
PY
python3 -c "import numpy; print('numpy', numpy.__version__, 'OK')" 2>/dev/null || {
  echo "numpy missing — trying pip3 install --user numpy"
  pip3 install --user numpy || {
    echo "ERROR: numpy install failed (offline box? use your distro: apt install python3-numpy)"; exit 1; }
}

say "test suite (the same 149+ tests CI runs — zero extra installs)"
python3 -m unittest discover -s tests

say "config"
if [ ! -f intel/.env ]; then
  cp intel/.env.example intel/.env
  echo "created intel/.env from the example — edit MI_SYMBOLS to YOUR broker's names"
else
  echo "intel/.env already present — leaving it alone"
fi

say "credential vault (optional, AES-256 at rest via system openssl)"
if command -v openssl >/dev/null; then
  if [ -t 0 ] && [ ! -f "${MI_VAULT_PATH:-$HOME/.config/mt5-research/vault.enc}" ]; then
    printf 'initialize the encrypted vault now? [y/N] '
    read -r yn || yn=n
    [ "$yn" = "y" ] && python3 tools/vault.py init || echo "later: python3 tools/vault.py init"
  else
    echo "later (or already present): python3 tools/vault.py init"
  fi
else
  echo "openssl not found — vault unavailable until it is installed"
fi

if [ "$WANT_SERVICE" = 1 ]; then
  say "systemd user service"
  bash intel/ops/install.sh
fi

say "next steps"
cat <<'STEPS'
1. Connect a broker terminal (DEMO account):
     Linux/macOS : Wine bridge  -> intel/docs/INSTALL-WINE-MT5.md
     Windows     : native       -> intel/docs/INSTALL-WINDOWS.md
2. Probe everything:        ./run.sh check
3. Gate YOUR broker's data: ./run.sh gate
4. Watch it think (no orders): ./run.sh observe
5. Autonomous (demo-gated, always): ./run.sh
   As a persistent service instead:  bash intel/ops/install.sh
   Dashboard: http://127.0.0.1:8877  ·  Emergency stop: touch intel/executor/data/KILL

SAFETY: every order path is demo-gated server-side. A real-money account gets
READ-ONLY service unless the live triple gate is deliberately opened
(GUIDE.md §7 — env + ALLOW_LIVE file + matching login). Trading contains
risk; nothing here is financial advice.
STEPS
