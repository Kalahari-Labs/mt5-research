# Install on Raspberry Pi (engine + dashboard on the Pi)

A Pi is a great 24/7 home for the **engine + dashboard**: silent, ~3W, always
on. The one thing a Pi cannot sensibly run is the **MT5 terminal itself** —
it is an x86 Windows program, and x86-under-ARM emulation (box64 + Wine) is
fragile exactly where you don't want fragility: the thing holding your orders.

So the supported Pi architecture is **split**:

```
┌────────────────────────────┐        ┌─────────────────────────────┐
│ any Windows PC / VPS,       │  HTTP  │ Raspberry Pi (24/7, ~3W)    │
│ or x86 Linux box with Wine  │◄──────►│  engine.py  (the brain)     │
│  MT5 terminal (demo login)  │  :8787 │  dashboard  :8877           │
│  bridge_server.py           │        │  SQLite journal             │
└────────────────────────────┘        └─────────────────────────────┘
```

The bridge machine only needs to be on while markets are open. A $5/month
Windows VPS also works — then nothing at home needs to stay on except the Pi.

## 1. Bridge machine (Windows or x86 Linux)

Set it up per [INSTALL-WINDOWS.md](INSTALL-WINDOWS.md) or
[INSTALL-WINE-MT5.md](INSTALL-WINE-MT5.md), then start the bridge listening on
the LAN interface instead of loopback:

```powershell
# Windows example — bind to this machine's LAN address:
set MI_BRIDGE_BIND=192.168.1.20
python intel\executor\bridge_server.py
```

Security notes, not optional:
- Bind to a specific LAN IP, never `0.0.0.0` on a machine with a public
  address. Anyone who can reach this port can place demo orders.
- Keep it inside your home network or a VPN/tailnet. Do not port-forward it.

## 2. Raspberry Pi (64-bit Raspberry Pi OS, Pi 3B+ or newer)

```bash
sudo apt update && sudo apt install -y python3-numpy git
git clone https://github.com/Kalahari-Labs/mt5-research && cd mt5-research
cp intel/.env.example intel/.env
nano intel/.env
```

Point the engine at the bridge machine and disable local spawning:

```ini
MI_BRIDGE_HOST=192.168.1.20   # the bridge machine's LAN IP
MI_BRIDGE_SPAWN=0             # never try to boot Wine/terminal on the Pi
MI_DASH_HOST=0.0.0.0          # so your phone/laptop can open the dashboard
```

Then exactly like every other platform:

```bash
./run.sh check     # verifies it can reach the remote bridge
./run.sh gate      # backtests on your broker's data (a few minutes on a Pi)
./run.sh observe   # watch the decision feed first
./run.sh           # autonomous
```

Dashboard from any device on your network: `http://<pi-ip>:8877`.

## 3. Survive reboots (systemd)

```bash
mkdir -p ~/.config/systemd/user
cp intel/ops/market-intel-executor.service ~/.config/systemd/user/
# edit the WorkingDirectory/ExecStart paths inside if your clone lives elsewhere
systemctl --user daemon-reload
systemctl --user enable --now market-intel-executor
loginctl enable-linger $USER
```

## Alternative: Docker on the Pi

The provided image is plain Python + numpy, so it builds fine on arm64:

```bash
docker compose up -d     # remember MI_BRIDGE_HOST must point at the bridge box
```

## What NOT to do on a Pi

Running the MT5 terminal on the Pi via box64/hangover emulation can be made to
boot, but a stack that must hold positions with real SL/TPs should not sit on
an emulator that can wedge mid-tick. If the bridge dies, the engine fails
safe (no new entries; broker-side SL/TP still protect open positions) — but
the right fix is a boring, reliable bridge machine.
