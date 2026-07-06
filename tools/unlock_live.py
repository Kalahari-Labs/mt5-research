#!/usr/bin/env python3
"""tools/unlock_live.py — the final gate to real money.

This script guides you through the risk disclosure and creates the magic file
required to bypass the demo-only gate.
"""
import sys
from pathlib import Path

# Try to find the executor directory regardless of where this is run from
BASE_DIR = Path(__file__).resolve().parent.parent / "intel" / "executor"
ALLOW_FILE = BASE_DIR / "ALLOW_LIVE"

def main():
    print("\n" + "!" * 80)
    print("!  WARNING: REAL-MONEY TRADING UNLOCK".center(80))
    print("!" * 80 + "\n")
    
    print("This script will create the final file required to allow the executor to send")
    print("orders to a LIVE (non-demo) trading account.")
    print("\nREQUIRED STEPS:")
    print("1. Set MI_ALLOW_LIVE=1 in your .env file")
    print("2. Run THIS script and enter your MT5 account login (the number)")
    print("3. Ensure your MT5 terminal is logged into a LIVE account in Wine/Windows")
    
    print("\n" + "=" * 80)
    print("DISCLOSURE: Trading leveraged products carries significant risk of loss.")
    print("Neither Kalahari Labs nor the AI assistant are responsible for your")
    print("financial decisions or losses. Demo results do not guarantee live profit.")
    print("=" * 80 + "\n")
    
    confirm = input("I understand and accept all risks. Type 'ACKNOWLEDGE' to proceed: ")
    if confirm != "ACKNOWLEDGE":
        print("Aborted.")
        return

    account_login = input("\nEnter your MT5 Account Login (digits only): ").strip()
    if not account_login.isdigit():
        print("Error: Login must be numeric.")
        return

    try:
        ALLOW_FILE.write_text(account_login)
        print(f"\nSUCCESS: Writing {ALLOW_FILE}")
        print("\nNEXT STEPS:")
        print(f"1. Open {BASE_DIR.parent}/.env")
        print("2. Add or update: MI_ALLOW_LIVE=1")
        print("3. Add or update: MI_HITL_MODE=1  (Manual User Approval highly recommended!)")
        print("4. Restart the bot: ./run.sh")
    except Exception as e:
        print(f"Error writing file: {e}")

if __name__ == "__main__":
    main()
