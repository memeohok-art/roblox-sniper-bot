"""Offline preflight — explains why the bot won't come online, without Discord.

    python doctor.py

Checks everything that commonly stops a deploy from connecting:
environment variables, token shape, cookie presence, writable state directory,
whether that directory survives a redeploy, and whether the dependencies import.

Exits 0 when the bot should be able to start.
"""

from __future__ import annotations

import os
import sys

OK = "✅"
WARN = "⚠️"
BAD = "❌"

problems: list[str] = []
warnings: list[str] = []


def line(mark: str, label: str, detail: str = "") -> None:
    print(f"{mark} {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    print("Roblox sniper preflight\n" + "-" * 46)

    # --- python -------------------------------------------------------------
    version = sys.version.split()[0]
    if sys.version_info < (3, 10):
        line(BAD, "Python", f"{version} — 3.10+ required")
        problems.append("Upgrade Python to 3.10 or newer.")
    else:
        line(OK, "Python", version)

    # --- dependencies -------------------------------------------------------
    missing = []
    for module in ("discord", "aiohttp", "dotenv"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        line(BAD, "Dependencies", "missing: " + ", ".join(missing))
        problems.append("Run: pip install -r requirements.txt")
    else:
        line(OK, "Dependencies", "discord.py, aiohttp, python-dotenv")

    # --- token --------------------------------------------------------------
    token = (os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or "").strip()
    if not token:
        line(BAD, "Discord token", "missing")
        problems.append("Set DISCORD_TOKEN in the environment (not just in .env on your laptop).")
    elif len(token) < 50 or "." not in token:
        line(WARN, "Discord token", f"unusual shape ({len(token)} chars)")
        warnings.append("Token looks truncated — copy it again from the Developer Portal.")
    else:
        line(OK, "Discord token", f"{len(token)} chars")

    # --- cookie -------------------------------------------------------------
    cookie = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
    if not cookie:
        line(BAD, "Roblox cookie", "missing")
        problems.append("Set ROBLOX_COOKIE; without it purchases fail (the bot still connects).")
    elif len(cookie) < 100:
        line(WARN, "Roblox cookie", f"short ({len(cookie)} chars) — probably truncated")
        warnings.append("Re-run connect_roblox.py: the cookie is shorter than a real one.")
    elif not cookie.lower().startswith("_|warning"):
        line(WARN, "Roblox cookie", "does not start with _|WARNING (still usable)")
    else:
        line(OK, "Roblox cookie", f"{len(cookie)} chars")

    # --- guild --------------------------------------------------------------
    guild = (os.getenv("GUILD_ID") or "").strip()
    if guild and not guild.isdigit():
        line(WARN, "Guild id", f"{guild!r} is not numeric")
        warnings.append("GUILD_ID must be the numeric server id.")
    elif guild:
        line(OK, "Guild id", guild + " (instant command sync)")
    else:
        line(WARN, "Guild id", "unset — global commands can take up to an hour")

    # --- state location -----------------------------------------------------
    from storage import analyse, at_risk, summary

    data_path = os.getenv("DATA_PATH", "data/state.json")
    report = analyse(data_path)
    if not report["writable"]:
        line(BAD, "State file", report.get("error", "not writable"))
        problems.append("Point DATA_PATH at a writable directory.")
    elif at_risk(report):
        line(BAD, "State file", summary(report))
        problems.append(
            "No volume mounted — bought items and sweep progress will be wiped on each "
            "redeploy. Mount a volume at /app/data (or wherever DATA_PATH points) and set "
            "DATA_PATH=/app/data/state.json."
        )
    else:
        line(OK, "State file", summary(report))

    # --- dry run ------------------------------------------------------------
    dry = (os.getenv("DRY_RUN") or "").strip().lower()
    if dry in ("", "true", "1", "yes", "on"):
        line(WARN, "Buying", "DRY_RUN on — nothing will actually be purchased")
        warnings.append("Set DRY_RUN=false when you are ready to buy.")
    else:
        line(OK, "Buying", "live")

    # --- verdict ------------------------------------------------------------
    print()
    if problems:
        print("✗ Not ready:")
        for p in problems:
            print(f"   • {p}")
    else:
        print("✓ Ready to start:  python bot.py")
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"   • {w}")

    print(
        "\nOn a host, remember: a worker/background process, not a web service —\n"
        "there is no HTTP port for a health check to probe."
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
