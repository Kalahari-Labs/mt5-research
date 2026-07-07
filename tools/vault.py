#!/usr/bin/env python3
"""tools/vault.py — encrypted-at-rest credential store (stdlib + system openssl).

Why this exists: broker logins, ntfy/Telegram tokens, and — one day, only
behind the live triple gate — real API keys must never sit in plaintext .env
files inside a public-repo checkout. This vault keeps them AES-256-CBC
(PBKDF2, 600k iterations, salted) encrypted in a single file OUTSIDE the
repo, chmod 0600.

No PyPI dependency: encryption is delegated to the system `openssl` binary,
which every target platform (Linux, macOS, WSL, Git-Bash) ships.

Usage:
  python3 tools/vault.py init                  create an empty vault
  python3 tools/vault.py set demo.login        value prompted, hidden
  python3 tools/vault.py set notify.ntfy_topic
  python3 tools/vault.py get demo.login
  python3 tools/vault.py list                  names only — never values
  python3 tools/vault.py export-env [PREFIX]   eval-able `export MI_...` lines
  python3 tools/vault.py rm notify.ntfy_topic

Conventions: namespace keys as demo.* / live.* / notify.* — `export-env demo`
emits only that namespace, so a demo service unit can never even see live.*.

Passphrase: prompted (hidden). Non-interactive callers (systemd, tests) set
MI_VAULT_PASSPHRASE in their own environment — an explicit, visible trade-off.
The passphrase is never accepted via argv (argv is world-readable in /proc).
"""
from __future__ import annotations

import getpass
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

VAULT_PATH = Path(os.environ.get(
    "MI_VAULT_PATH", "~/.config/mt5-research/vault.enc")).expanduser()
PBKDF2_ITERS = "600000"


def die(msg: str, code: int = 1) -> "None":
    print("vault: %s" % msg, file=sys.stderr)
    raise SystemExit(code)


def passphrase(confirm: bool = False) -> str:
    pw = os.environ.get("MI_VAULT_PASSPHRASE")
    if pw:
        return pw
    if not sys.stdin.isatty():
        die("no TTY and MI_VAULT_PASSPHRASE not set")
    pw = getpass.getpass("vault passphrase: ")
    if not pw:
        die("empty passphrase refused")
    if confirm and getpass.getpass("confirm passphrase: ") != pw:
        die("passphrases do not match")
    return pw


def _openssl(extra: list[str], data: bytes, pw: str) -> bytes:
    """Run openssl enc with the passphrase passed via the child's environment
    only — never argv. Non-zero exit (wrong passphrase, corrupt file) dies."""
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", PBKDF2_ITERS,
         "-salt", "-pass", "env:_MI_VAULT_PASS"] + extra,
        input=data, capture_output=True,
        env=dict(os.environ, _MI_VAULT_PASS=pw))
    if proc.returncode != 0:
        die("openssl failed (wrong passphrase or corrupt vault): %s"
            % proc.stderr.decode(errors="replace").strip()[:200])
    return proc.stdout


def read_vault(pw: str) -> dict:
    if not VAULT_PATH.exists():
        die("no vault at %s — run: python3 tools/vault.py init" % VAULT_PATH)
    return json.loads(_openssl(["-d"], VAULT_PATH.read_bytes(), pw))


def write_vault(entries: dict, pw: str) -> None:
    VAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(VAULT_PATH.parent, 0o700)
    blob = _openssl([], json.dumps(entries, indent=0).encode(), pw)
    # atomic replace so a crash mid-write can never truncate the vault
    fd, tmp = tempfile.mkstemp(dir=str(VAULT_PATH.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        os.chmod(tmp, 0o600)
        os.replace(tmp, VAULT_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def env_name(key: str) -> str:
    return "MI_" + key.upper().replace(".", "_").replace("-", "_")


def main(argv: list[str]) -> None:
    if len(argv) < 1:
        die(__doc__.strip().splitlines()[0] + " — see file header for usage")
    cmd, args = argv[0], argv[1:]

    if cmd == "init":
        if VAULT_PATH.exists():
            die("vault already exists at %s" % VAULT_PATH)
        write_vault({}, passphrase(confirm=True))
        print("vault created: %s (0600)" % VAULT_PATH)

    elif cmd == "set":
        if len(args) != 1:
            die("usage: set NAME")
        pw = passphrase()
        entries = read_vault(pw)
        value = os.environ.get("MI_VAULT_VALUE")
        if value is None:
            if not sys.stdin.isatty():
                die("no TTY and MI_VAULT_VALUE not set")
            value = getpass.getpass("value for %s (hidden): " % args[0])
        if not value:
            die("empty value refused")
        entries[args[0]] = value
        write_vault(entries, pw)
        print("stored: %s" % args[0])

    elif cmd == "get":
        if len(args) != 1:
            die("usage: get NAME")
        entries = read_vault(passphrase())
        if args[0] not in entries:
            die("no such key: %s" % args[0])
        print(entries[args[0]])

    elif cmd == "rm":
        if len(args) != 1:
            die("usage: rm NAME")
        pw = passphrase()
        entries = read_vault(pw)
        if entries.pop(args[0], None) is None:
            die("no such key: %s" % args[0])
        write_vault(entries, pw)
        print("removed: %s" % args[0])

    elif cmd == "list":
        for name in sorted(read_vault(passphrase())):
            print(name)

    elif cmd == "export-env":
        prefix = (args[0].rstrip(".") + ".") if args else ""
        entries = read_vault(passphrase())
        for name in sorted(entries):
            if prefix and not name.startswith(prefix):
                continue
            print("export %s=%s" % (env_name(name), shlex.quote(entries[name])))

    else:
        die("unknown command: %s (init|set|get|rm|list|export-env)" % cmd)


if __name__ == "__main__":
    main(sys.argv[1:])
