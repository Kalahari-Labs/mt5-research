# From-scratch install — Linux + Wine + MT5 + the executor

Reference setup this was built and verified on: Ubuntu-family Linux,
**Wine 11.10 (Staging)**, Windows **Python 3.12**, **MetaTrader5 pip
5.0.5735**, **numpy 2.4.6** (Wine side), XM Global MT5 **demo** account.
Any MT5 broker with demo accounts works — symbol names differ per broker
(that is what `MI_SYMBOLS` and the onboarding checker are for).

Total time on a fresh machine: ~30-45 min, most of it downloads.

## 0. Headless server (no monitor)? Read this first

Wine needs an X display to open the MT5 terminal's GUI — a bare Ubuntu
Server box has none, and a systemd service has no `DISPLAY` even if you
happened to have one in your SSH session. Set up a virtual one before step 2:

```bash
sudo apt install xvfb x11vnc
Xvfb :99 -screen 0 1280x800x24 &        # keep this running permanently
export DISPLAY=:99
```

For the one-time steps in section 3 (log into the demo account, click
Enable AutoTrading) you need to actually *see* that virtual display:

```bash
x11vnc -display :99 -nopw -listen 0.0.0.0 -xkb &
```

then connect from your desktop with any VNC client to `<server-ip>:5900`.
Once logged in with AutoTrading green you can kill the VNC session —
Xvfb itself must keep running for as long as the executor does.

For ongoing/service operation (section 5), set `MI_WINE_DISPLAY=:99` in
`intel/.env` so the engine passes `DISPLAY` to Wine explicitly — a systemd
user service does not inherit your shell's exported `DISPLAY`.

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

In the terminal GUI (see section 0 above if this is a headless server):
1. Open a **demo** account (or log into an existing one).
2. **Enable AutoTrading** (Ctrl+E — the toolbar button must be green).
3. Leave the terminal running. The executor attaches to whatever this
   terminal is logged into — and its bridge refuses to trade anything that
   is not a demo account.

## 4. The executor

```bash
git clone https://github.com/Kalahari-Labs/mt5-research.git
cd mt5-research
cp intel/.env.example intel/.env     # review the knobs, especially MI_SYMBOLS
                                      # headless server: also set MI_WINE_DISPLAY=:99

./run.sh check         # checks EVERYTHING above with live probes
./run.sh gate          # backtests all combos on YOUR broker's data
./run.sh observe       # watch the decision feed first (no orders)
./run.sh               # autonomous, demo-gated
```

Dashboard: http://127.0.0.1:8877 · Emergency stop: `touch intel/executor/data/KILL`

`./run.sh check` is the source of truth — every FAIL line tells you the
exact fix. Broker symbol names are the most common snag (e.g. gold is
`GOLD` on XM but `XAUUSD` on most others → edit `MI_SYMBOLS` in
`intel/.env`, and add the mapping to `SYMBOL_CURRENCIES` in
`intel/executor/config.py` if it's not a default symbol, so the news
blackout covers it).

## 5. Keep it running (optional)

```bash
bash intel/ops/install.sh
```

Installs a systemd **user** service (`market-intel-executor`) that restarts
on crash and survives reboot (via `loginctl enable-linger`). If this is a
headless server, make sure `MI_WINE_DISPLAY` is set in `intel/.env` first
(section 0) and that Xvfb itself is kept running permanently — e.g. its own
tiny systemd service, or `@reboot` in cron — since the executor service will
try to launch Wine against it on every restart.

```bash
systemctl --user status  market-intel-executor
journalctl --user -u market-intel-executor -f
```

## Read before trusting it with anything

[executor/EXECUTOR.md](../executor/EXECUTOR.md) — the safety model, the
honesty model, and the risk disclosure. Short version: demo is the product;
the gate will refuse most strategies (that is it working); the 90-day
forward test in [forward-test.md](forward-test.md) is the only scoreboard.
