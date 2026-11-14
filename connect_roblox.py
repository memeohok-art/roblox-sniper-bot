"""Connect your Roblox account the easy way — log in in a real browser window,
and this script pulls the `.ROBLOSECURITY` cookie out and stores it for you.

    python connect_roblox.py              # browser login -> writes .env
    python connect_roblox.py --print      # also prints the cookie (for a host's env var)
    python connect_roblox.py --timeout 600

First run needs Playwright's Chromium (one-time, ~130 MB):

    pip install playwright
    playwright install chromium

Nothing is sent anywhere except to roblox.com itself (to confirm which account
the cookie belongs to).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
import time

LOGIN_URL = "https://www.roblox.com/login"
COOKIE_NAME = ".ROBLOSECURITY"
COOKIE_DOMAIN = "https://www.roblox.com"


class SetupError(RuntimeError):
    """Playwright/browser is missing — distinct from "the user didn't log in"."""


def say(*parts: str) -> None:
    print(*parts, flush=True)


def mask(value: str, keep: int = 8) -> str:
    """Enough to recognise a cookie without exposing it."""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-4:]} ({len(value)} chars)"


def write_env_cookie(path: str, cookie: str) -> None:
    """Set ROBLOX_COOKIE in .env, preserving every other line."""
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    else:
        lines = []

    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("ROBLOX_COOKIE="):
            lines[i] = f"ROBLOX_COOKIE={cookie}"
            replaced = True

    if not replaced:
        lines.append(f"ROBLOX_COOKIE={cookie}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows


async def _whoami(cookie: str) -> tuple[bool, str]:
    from roblox_api import RobloxClient, RobloxError

    client = RobloxClient(cookie)
    try:
        await client.start()
        data = await client.authenticate()
        return True, f"{data.get('name')} (id {data.get('id')})"
    except RobloxError as exc:
        return False, str(exc)
    except Exception as exc:  # network/DNS and friends
        return False, f"could not reach Roblox: {exc}"
    finally:
        await client.close()


def _capture_cookie(timeout: float) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SetupError(
            "Playwright is not installed.\n\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=False, args=["--start-maximized"])
        except Exception as exc:
            detail = str(exc).splitlines()[0]
            if "XServer" in str(exc) or "headed" in str(exc).lower():
                raise SetupError(
                    "This opens a real browser window, so it has to run on a desktop —\n"
                    "  not on a headless server/SSH session. Use the DevTools cookie method\n"
                    "  from the README there, then set ROBLOX_COOKIE by hand."
                )
            raise SetupError(
                f"Could not start a browser: {detail}\n  Run:  playwright install chromium"
            )

        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception:
            pass  # the window is open; the user can navigate manually

        say("→ A browser window is open. Log into Roblox there.")
        say("  Finish any 2FA prompt. This script detects the cookie automatically.\n")

        deadline = time.time() + timeout
        cookie: str | None = None
        while time.time() < deadline:
            try:
                for entry in context.cookies(COOKIE_DOMAIN):
                    if entry.get("name") == COOKIE_NAME and entry.get("value"):
                        cookie = entry["value"]
                        break
            except Exception:
                pass
            if cookie:
                break
            try:
                page.wait_for_timeout(700)
            except Exception:
                time.sleep(0.7)

        browser.close()
        return cookie


def main() -> int:
    parser = argparse.ArgumentParser(description="Connect a Roblox account to the sniper.")
    parser.add_argument("--timeout", type=float, default=300, help="seconds to wait for login (default 300)")
    parser.add_argument("--print", dest="print_cookie", action="store_true",
                        help="print the cookie so you can paste it into a host's env vars")
    parser.add_argument("--env-file", default=".env", help="where to store the cookie (default .env)")
    parser.add_argument("--no-verify", action="store_true", help="skip the account check")
    args = parser.parse_args()

    logging.getLogger("sniper").setLevel(logging.CRITICAL)

    say("Roblox account connect\n" + "-" * 40)
    try:
        cookie = _capture_cookie(args.timeout)
    except SetupError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 1
    if not cookie:
        print("\n✗ No login detected.", file=sys.stderr)
        print("  Try again, or copy the cookie by hand (README → Connecting your Roblox account).",
              file=sys.stderr)
        return 1

    print(f"✓ Captured {COOKIE_NAME}: {mask(cookie)}")

    if not args.no_verify:
        ok, detail = asyncio.run(_whoami(cookie))
        if ok:
            print(f"✓ Signed in as {detail}")
        else:
            print(f"⚠ Roblox did not accept the cookie: {detail}")
            print("  (It may still work later — Roblox sometimes rate-limits checks.)")

    write_env_cookie(args.env_file, cookie)
    print(f"✓ Written to {args.env_file} (permissions 600)")

    if args.print_cookie:
        print("\nCopy this into your host's ROBLOX_COOKIE variable — treat it like a password:\n")
        print(cookie)
        print()

    print("\nNext:  python selftest.py   →   python bot.py   →   /snipe doctor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
