# From-scratch install — Linux + Wine + MT5 + the executor

Reference setup this was built and verified on: Ubuntu-family Linux,
**Wine 11.10 (Staging)**, Windows **Python 3.12**, **MetaTrader5 pip
5.0.5735**, **numpy 2.4.6** (Wine side), XM Global MT5 **demo** account.
Any MT5 broker with demo accounts works — symbol names differ per broker
(that is what `MI_SYMBOLS` and the onboarding checker are for).

Total time on a fresh machine: ~30-45 min, most of it downloads.

## 1. Linux packages

```bash
# Wine (winehq staging or stable, either works)
sudo dpkg --add-architecture i386
sudo mkdir -pm755 /etc/apt/keyrings
sudo wget -O /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
# pick your distro codename file from https://dl.winehq.org/wine-builds/ubuntu/dists/
sudo wget -NP /etc/apt/sources.list.d/ https://dl.winehq.org/wine-builds/ubuntu/dists/$(lsb_release -cs)/winehq-$(lsb_release -cs).sources
sudo apt update && sudo apt install --install-recommends winehq-staging

# host-side Python needs numpy only
sudo apt install python3-numpy git
```

## 2. Wine prefix with Windows Python

```bash
export WINEPREFIX=$HOME/.mt5     # the executor's default; keep it
winecfg                          # first run creates the prefix; set Windows 10, close

# Windows Python 3.12 (64-bit) inside the prefix
wget https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
WINEPREFIX=$HOME/.mt5 wine python-3.12.10-amd64.exe /quiet InstallAllUsers=1 PrependPath=1

# MetaTrader5 package (pulls its own numpy)
WINEPREFIX=$HOME/.mt5 WINEDEBUG=-all wine "C:\\Program Files\\Python312\\python.exe" -m pip install MetaTrader5
```

If the silent installer misbehaves, run it without `/quiet` and click through —
just keep "Install for all users" so the path is `C:\Program Files\Python312`.
(A different path is fine too: set `MI_WINE_PYTHON` in `.env`.)

## 3. MetaTrader 5 terminal + demo account

```bash
# download mt5setup.exe from your broker (XM, IC Markets, Pepperstone, ...)
WINEPREFIX=$HOME/.mt5 wine mt5setup.exe
```

In the terminal GUI:
1. Open a **demo** account (or log into an existing one).
2. **Enable AutoTrading** (Ctrl+E — the toolbar button must be green).
3. Leave the terminal running. The executor attaches to whatever this
   terminal is logged into — and its bridge refuses to trade anything that
   is not a demo account.

## 4. The executor

```bash
git clone https://github.com/Kalahari-Labs/market-intel
cd market-intel
cp .env.example .env             # review the knobs, especially MI_SYMBOLS

python3 -m executor.onboard      # checks EVERYTHING above with live probes
python3 -m executor.gate         # backtests all combos on YOUR broker's data
./start.sh observe               # watch the decision feed first (no orders)
./start.sh                       # autonomous, demo-gated
```

Dashboard: http://127.0.0.1:8877 · Emergency stop: `touch executor/data/KILL`

`python3 -m executor.onboard` is the source of truth — every FAIL line tells
you the exact fix. Broker symbol names are the most common snag (e.g. gold is
`GOLD` on XM but `XAUUSD` on most others → edit `MI_SYMBOLS` in `.env`, and
add the mapping to `SYMBOL_CURRENCIES` in `executor/config.py` if it's not a
default symbol, so the news blackout covers it).

## 5. Keep it running (optional)

```bash
cp ops/market-intel-executor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now market-intel-executor
loginctl enable-linger $USER
journalctl --user -u market-intel-executor -f      # or tail -f logs/engine.log
```

## Read before trusting it with anything

[executor/EXECUTOR.md](../executor/EXECUTOR.md) — the safety model, the
honesty model, and the risk disclosure. Short version: demo is the product;
the gate will refuse most strategies (that is it working); the 90-day
forward test in [forward-test.md](forward-test.md) is the only scoreboard.
