#!/usr/bin/env bash
# One-command setup for the Roblox sniper bot (macOS / Linux).
#
#   ./setup.sh            install, configure, verify, start
#   ./setup.sh --no-run   install and configure only
#
# Safe to re-run: an existing .env is never overwritten.

set -euo pipefail
cd "$(dirname "$0")"

NO_RUN=0
[ "${1:-}" = "--no-run" ] && NO_RUN=1

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "✗ Python 3 not found. Install Python 3.10+ and re-run." >&2
  exit 1
fi

"$PY" - <<'PYCHECK'
import sys
if sys.version_info < (3, 10):
    sys.exit("✗ Python 3.10+ required, found %s" % sys.version.split()[0])
PYCHECK
echo "✓ $("$PY" -c 'import sys; print("Python " + sys.version.split()[0])')"

# ---------------------------------------------------------------- virtualenv
FALLBACK=0
if [ ! -d .venv ]; then
  echo "→ Creating .venv"
  if ! "$PY" -m venv .venv >/tmp/sniper-venv.log 2>&1; then
    rm -rf .venv
    FALLBACK=1
    echo "⚠ Could not create a virtualenv (details: /tmp/sniper-venv.log)"
    echo "  On Debian/Ubuntu:  sudo apt install python3-venv"
    echo "→ Falling back to user site-packages (~/.local)"
  fi
fi

if [ "$FALLBACK" -eq 0 ]; then
  # shellcheck disable=SC1091
  . .venv/bin/activate
  PYBIN=python
else
  PYBIN="$PY"
fi

echo "→ Installing dependencies"
if [ "$FALLBACK" -eq 0 ]; then
  "$PYBIN" -m pip install --quiet --upgrade pip
  "$PYBIN" -m pip install --quiet -r requirements.txt
else
  "$PYBIN" -m pip install --user --quiet -r requirements.txt 2>/dev/null \
    || "$PYBIN" -m pip install --user --quiet --break-system-packages -r requirements.txt
fi
echo "✓ Dependencies installed"

# --------------------------------------------------------------------- .env
[ -f .env ] || cp .env.example .env
"$PYBIN" - <<'PYENV'
import getpass
import os
import re
import sys

path = ".env"
text = open(path, encoding="utf-8").read()


def current(key):
    match = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else ""


def ask(prompt, secret=True):
    if not sys.stdin.isatty():
        return ""
    try:
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


changed = []

if not current("DISCORD_TOKEN") and not current("TOKEN"):
    token = ask("Paste your Discord bot token (input hidden): ")
    if token:
        text = re.sub(r"^DISCORD_TOKEN=.*$", "DISCORD_TOKEN=" + token, text, flags=re.MULTILINE)
        changed.append("DISCORD_TOKEN")
    else:
        print("! No token entered — set DISCORD_TOKEN in .env later")

if not current("ROBLOX_COOKIE"):
    cookie = ask("Paste the .ROBLOSECURITY value of the throwaway account (hidden): ")
    if cookie:
        text = re.sub(r"^ROBLOX_COOKIE=.*$", "ROBLOX_COOKIE=" + cookie, text, flags=re.MULTILINE)
        changed.append("ROBLOX_COOKIE")
    else:
        print("! No cookie entered — /snipe doctor will report a dead cookie until you add one")

if not current("GUILD_ID") and sys.stdin.isatty():
    guild = ask("Your Discord server id (optional, Enter to skip): ", secret=False)
    if guild.isdigit():
        text = re.sub(r"^GUILD_ID=.*$", "GUILD_ID=" + guild, text, flags=re.MULTILINE)
        changed.append("GUILD_ID")

open(path, "w", encoding="utf-8").write(text)
os.chmod(path, 0o600)
print(f"✓ .env updated ({', '.join(changed) if changed else 'no changes'}), permissions 600")
PYENV

if ! grep -qE '^ROBLOX_COOKIE=.+' .env 2>/dev/null; then
  echo
  echo "→ No Roblox cookie yet."
  echo "  Easiest way:  \"$PYBIN\" connect_roblox.py   (browser login, no DevTools)"
  echo "  It needs:     \"$PYBIN\" -m pip install -r requirements-connect.txt && playwright install chromium"
fi

# ------------------------------------------------------------------- verify
echo "→ Running offline self-test"
if ! "$PYBIN" selftest.py >/tmp/sniper-selftest.log 2>&1; then
  tail -25 /tmp/sniper-selftest.log
  echo "✗ Self-test failed — fix the above before running the bot." >&2
  exit 1
fi
tail -2 /tmp/sniper-selftest.log
echo "✓ Self-test passed"

# -------------------------------------------------------------------- start
if [ "$NO_RUN" -eq 1 ]; then
  echo
  if [ "$FALLBACK" -eq 0 ]; then
    echo "Setup complete. Start with:  source .venv/bin/activate && python bot.py"
  else
    echo "Setup complete. Start with:  $PYBIN bot.py"
  fi
  exit 0
fi

echo
echo "→ Starting the bot (Ctrl-C to stop)"
exec "$PYBIN" bot.py
