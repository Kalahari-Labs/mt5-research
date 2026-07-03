# Install on Windows (native — no Wine needed)

On Windows the MT5 terminal and the bridge run natively; the executor uses the
same Python for everything.

> **Before anything:** read [executor/EXECUTOR.md](../executor/EXECUTOR.md),
> especially the risk disclosure. Use a **demo account**. The bridge refuses
> to place orders on anything else unless you deliberately unlock it.

## 1. Install the pieces

1. **Python 3.10+** from [python.org](https://www.python.org/downloads/)
   (check "Add python.exe to PATH" in the installer).
2. **MetaTrader 5** from your broker (any broker with MT5 demo accounts works
   — XM, IC Markets, Pepperstone, FTMO-style props, etc.).
3. Open MT5, **log into a DEMO account**, and enable AutoTrading (Ctrl+E —
   the button must be green).
4. In PowerShell or cmd:

   ```powershell
   git clone https://github.com/kalahari-labs/mt5-research
   cd mt5-research
   pip install numpy MetaTrader5
   ```

## 2. Configure

```powershell
copy intel\.env.example intel\.env
notepad intel\.env
```

Set `MI_SYMBOLS` to *your broker's* symbol names (brokers suffix them:
`EURUSD`, `EURUSDm`, `EURUSD.a` ...). Leave risk defaults alone until you have
watched it run.

If MT5 is not in the default location, set
`MI_TERMINAL_EXE=C:\path\to\terminal64.exe` in `intel\.env`.

## 3. Verify, observe, then run

```powershell
cd intel
python -m executor.onboard       # probes every prerequisite, tells you what to fix
python -m executor.gate          # backtests every strategy x symbol on YOUR broker's data
set MI_EXEC_MODE=observe && python -m executor.run   # full pipeline, zero orders
```

Watch the dashboard at http://127.0.0.1:8877 for a while. When you understand
what it skips and why:

```powershell
python -m executor.run           # autonomous (demo-gated server-side regardless)
```

Stop all new entries instantly at any time:

```powershell
type nul > executor\data\KILL    # flatten everything + halt; delete the file to resume
```

## Run at startup (optional)

Task Scheduler → Create Basic Task → At log on →
Program: `python` · Arguments: `-m executor.run` · Start in: `C:\...\mt5-research\intel`.

## macOS note

There is no native MT5 for macOS. Either run the terminal + bridge under Wine
(see [INSTALL-WINE-MT5.md](INSTALL-WINE-MT5.md) — same steps work with
`brew install --cask wine-stable`), or run the terminal on any Windows
machine/VPS, start the bridge there with `MI_BRIDGE_BIND` set to a reachable
interface, and point the engine at it with `MI_BRIDGE_HOST=<that-ip>` and
`MI_BRIDGE_SPAWN=0`.
