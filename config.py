"""Environment configuration. Everything secret comes from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    discord_token: str
    roblox_cookie: str
    dry_run: bool
    data_path: str
    request_gap: float
    hot_gap: float
    max_in_flight: int
    log_level: str
    guild_id: int | None

    @classmethod
    def load(cls) -> "Settings":
        # DISCORD_TOKEN is the documented name; TOKEN is accepted as an alias so a
        # secret already stored under that name works without being copied anywhere.
        token = (os.getenv("DISCORD_TOKEN") or os.getenv("TOKEN") or "").strip()
        cookie = (os.getenv("ROBLOX_COOKIE") or os.getenv("ROBLOSECURITY") or "").strip()
        if not token:
            raise SystemExit("No bot token found — set DISCORD_TOKEN (or TOKEN) in .env")
        if not cookie:
            raise SystemExit("ROBLOX_COOKIE is missing — see README (it must be a .ROBLOSECURITY value)")
        guild_raw = (os.getenv("GUILD_ID") or "").strip()
        return cls(
            discord_token=token,
            roblox_cookie=cookie,
            dry_run=_bool("DRY_RUN", False),
            data_path=os.getenv("DATA_PATH", "data/state.json"),
            request_gap=_float("REQUEST_GAP", 1.2),
            hot_gap=_float("HOT_GAP", 0.35),
            max_in_flight=_int("MAX_IN_FLIGHT", 4),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            guild_id=int(guild_raw) if guild_raw.isdigit() else None,
        )
