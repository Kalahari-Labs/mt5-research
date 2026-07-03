@echo off
rem ONE command, whole stack on Windows: MT5 terminal -> bridge -> engine -> dashboard.
rem
rem   run.bat            autonomous trading (demo-gated server-side, always)
rem   run.bat observe    full pipeline, journals every decision, sends NO orders
rem   run.bat check      onboarding probe only - tells you what's missing
rem   run.bat gate       backtest every strategy x symbol on YOUR broker's data
cd /d "%~dp0intel"

where python >nul 2>nul || (echo python not found - install Python 3.10+ from python.org & exit /b 1)
python -c "import numpy" 2>nul || (echo numpy missing - run: pip install numpy MetaTrader5 & exit /b 1)

if not exist .env (
  copy .env.example .env >nul
  echo *** created intel\.env from the example - edit MI_SYMBOLS to YOUR broker's symbol names.
)

if "%1"=="check"   ( python -m executor.onboard & exit /b )
if "%1"=="gate"    ( python -m executor.gate & exit /b )
if "%1"=="observe" ( set MI_EXEC_MODE=observe& python -m executor.run & exit /b )
python -m executor.run
