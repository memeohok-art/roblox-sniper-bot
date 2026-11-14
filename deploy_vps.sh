#!/usr/bin/env bash
# Deploy the sniper on any Linux box that stays on — a free Oracle/VPS instance,
# a Raspberry Pi, an old laptop, a home server.
#
#   curl -fsSL ... | bash          (or:  sudo bash deploy_vps.sh )
#
# Installs it to /opt/roblox-sniper, creates a venv, asks once for the token and
# cookie, registers a systemd service with Restart=always, and starts it.
#
# Re-running is safe: the install directory and .env are preserved.

set -euo pipefail

TARGET="${TARGET:-/opt/roblox-sniper}"
SERVICE="${SERVICE:-roblox-sniper}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OWNER="${SUDO_USER:-$(id -un)}"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*" >&2; exit 1; }

if [ "${NO_SERVICE:-0}" != "1" ] && [ "$(id -u)" -ne 0 ]; then
  die "Run with sudo (needs to write /opt and register the systemd service)."
fi

if [ "${NO_SERVICE:-0}" != "1" ]; then
  command -v systemctl >/dev/null || die "systemd not found — this script targets systemd Linux."
fi

# --------------------------------------------------------------- prerequisites
if ! command -v python3 >/dev/null || ! python3 -c 'import venv' 2>/dev/null; then
  say "→ Installing python3 + venv"
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq python3 python3-venv python3-pip
  elif command -v dnf >/dev/null; then
    dnf install -y -q python3 python3-pip
  else
    die "Install python3 and python3-venv manually, then re-run."
  fi
fi

python3 - <<'PY' || die "Python 3.10+ is required."
import sys
if sys.version_info < (3, 10):
    sys.exit(1)
PY

# -------------------------------------------------------------------- install
say "→ Installing to $TARGET"
mkdir -p "$TARGET"
if [ "$SRC" != "$TARGET" ]; then
  # Copy sources but never clobber an existing .env or the data directory.
  for f in bot.py roblox_api.py store.py config.py connect_roblox.py selftest.py \
           requirements.txt .env.example setup.sh README.md Dockerfile; do
    [ -f "$SRC/$f" ] && cp -f "$SRC/$f" "$TARGET/"
  done
fi
[ "$(id -u)" -eq 0 ] && chown -R "$OWNER":"$OWNER" "$TARGET" 2>/dev/null || true

if [ ! -d "$TARGET/.venv" ]; then
  say "→ Creating virtualenv"
  if ! python3 -m venv "$TARGET/.venv" >/tmp/sniper-venv.log 2>&1; then
    rm -rf "$TARGET/.venv"
    # Debian/Ubuntu ship a separate package for ensurepip; install it and retry.
    say "→ venv failed, installing python3-venv"
    if command -v apt-get >/dev/null; then
      apt-get update -qq && apt-get install -y -qq python3-venv
    elif command -v dnf >/dev/null; then
      dnf install -y -q python3-virtualenv || true
    fi
    python3 -m venv "$TARGET/.venv" || die "Could not create a venv (see /tmp/sniper-venv.log)."
  fi
fi
"$TARGET/.venv/bin/pip" install --quiet --upgrade pip
"$TARGET/.venv/bin/pip" install --quiet -r "$TARGET/requirements.txt"
say "✓ Dependencies installed"

# ----------------------------------------------------------------------- .env
if [ ! -f "$TARGET/.env" ]; then
  if [ -f "$TARGET/.env.example" ]; then
    cp "$TARGET/.env.example" "$TARGET/.env"
  else
    printf 'DISCORD_TOKEN=\nROBLOX_COOKIE=\nDRY_RUN=true\nLOG_LEVEL=INFO\n' > "$TARGET/.env"
  fi
fi

"$TARGET/.venv/bin/python" - "$TARGET/.env" <<'PY'
import getpass, re, sys

path = sys.argv[1]
text = open(path, encoding="utf-8").read()


def val(key):
    m = re.search(rf"^{key}=(.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def ask(prompt):
    if not sys.stdin.isatty():
        return ""
    try:
        return getpass.getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


changed = []
if not val("DISCORD_TOKEN"):
    token = ask("Discord bot token (hidden): ")
    if token:
        text = re.sub(r"^DISCORD_TOKEN=.*$", f"DISCORD_TOKEN={token}", text, flags=re.MULTILINE)
        changed.append("DISCORD_TOKEN")

if not val("ROBLOX_COOKIE"):
    cookie = ask("Roblox .ROBLOSECURITY cookie (hidden): ")
    if cookie:
        text = re.sub(r"^ROBLOX_COOKIE=.*$", f"ROBLOX_COOKIE={cookie}", text, flags=re.MULTILINE)
        changed.append("ROBLOX_COOKIE")

if not val("DATA_PATH"):
    text += f"DATA_PATH={path.rsplit('/', 1)[0]}/data/state.json\n"
    changed.append("DATA_PATH")

open(path, "w", encoding="utf-8").write(text)
import os
os.chmod(path, 0o600)
print(f"✓ .env ready ({', '.join(changed) if changed else 'unchanged'})")
PY

mkdir -p "$TARGET/data"
[ "$(id -u)" -eq 0 ] && chown -R "$OWNER":"$OWNER" "$TARGET/data" || true

if [ "${NO_SERVICE:-0}" = "1" ]; then
  say "→ NO_SERVICE=1: skipping systemd"
  "$TARGET/.venv/bin/python" "$TARGET/selftest.py" | tail -3
  say "Start manually: cd $TARGET && ./.venv/bin/python bot.py"
  exit 0
fi

# -------------------------------------------------------------------- service
say "→ Writing systemd service"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Roblox free-item sniper bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$OWNER
WorkingDirectory=$TARGET
Environment=PYTHONUNBUFFERED=1
ExecStart=$TARGET/.venv/bin/python $TARGET/bot.py
Restart=always
RestartSec=10
# Keep it dumb: crash, wait, restart. The sweep cursor survives restarts.
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

sleep 3
if systemctl is-active --quiet "$SERVICE"; then
  say "✓ Running as systemd service '$SERVICE' (auto-starts on boot, restarts on crash)"
else
  say "⚠ Service did not stay up — check:  journalctl -u $SERVICE -n 50 --no-pager"
fi

cat <<MSG

Done.

  Status:  systemctl status $SERVICE
  Logs:    journalctl -u $SERVICE -f
  Restart: systemctl restart $SERVICE
  Stop:    systemctl stop $SERVICE
  Config:  $TARGET/.env   (edit, then: systemctl restart $SERVICE)

In Discord:  /snipe whoami   then   /snipe doctor
When doctor is green: set DRY_RUN=false in $TARGET/.env and restart.
MSG
