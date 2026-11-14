"""Tiny JSON-backed state store (one file, atomic writes, no dependencies).

Holds per-guild sniper config plus the "already handled" id sets and purchase
history. Good enough for a self-hosted bot; swap for SQLite if you scale up.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

SEEN_LIMIT = 20000   # a full catalog sweep can buy thousands
HISTORY_LIMIT = 200


def defaults() -> dict[str, Any]:
    return {
        "enabled": False,
        "channel_id": None,
        "interval": 8.0,          # seconds between scans
        "fast": False,            # purchase-first polling of the watch list
        "hot_interval": 1.0,      # seconds between hot polls in fast mode
        "pages": 2,               # catalog pages per scan (30 items each)
        "category": 1,            # 1 = All, 11 = Accessories, 3 = Clothing, 2 = Collectibles
        "subcategory": None,
        "sort_type": 3,           # 3 = recently updated, 0 = relevance, 2 = sales
        "sales_type_filter": 1,
        "keyword": None,
        "exclude_keywords": [],
        "asset_types": [],        # e.g. [8, 41, 42]; empty = any
        "watch_ids": [],          # always re-check these item ids
        "watch_meta": {},         # id -> cached product/seller fields for fast purchases
        "max_purchases_per_cycle": 3,
        "seen": [],               # ids already bought / permanently handled
        "cooldowns": {},          # id -> unix ts before which we retry
        "history": [],
        "sweep": {
            "active": False,
            "cursor": None,
            "category": 1,
            "sort_type": 3,
            "limit": 0,          # 0 = no purchase cap
            "max_pages": 500,    # pages per run (30 items each), resumable
            "pages": 0,
            "scanned": 0,
            "matched": 0,
            "verified": 0,
            "bought": 0,
            "owned": 0,
            "failed": 0,
            "started_at": None,
            "finished_at": None,
            "reason": None,
            "last_error": None,
        },
    }


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"guilds": {}}
        self.load()

    # -------------------------------------------------------------- file IO
    def load(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        if not os.path.exists(self.path):
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("guilds"), dict):
                self._data = data
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Could not read %s (%s) — starting fresh", self.path, exc)
            self._data = {"guilds": {}}

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    # ------------------------------------------------------------- accessors
    def get(self, guild_id: int | str) -> dict[str, Any]:
        key = str(guild_id)
        cfg = self._data["guilds"].get(key)
        if not isinstance(cfg, dict):
            cfg = defaults()
            self._data["guilds"][key] = cfg
            self.save()
            return cfg
        merged = defaults()
        merged.update(cfg)
        self._data["guilds"][key] = merged
        return merged

    def update(self, guild_id: int | str, **changes: Any) -> dict[str, Any]:
        cfg = self.get(guild_id)
        cfg.update(changes)
        self.save()
        return cfg

    def all_guilds(self) -> dict[str, dict[str, Any]]:
        return self._data["guilds"]

    # ------------------------------------------------------------ snipe data
    def is_seen(self, guild_id: int | str, item_id: int) -> bool:
        return item_id in set(self.get(guild_id).get("seen", []))

    def on_cooldown(self, guild_id: int | str, item_id: int) -> bool:
        expiry = self.get(guild_id).get("cooldowns", {}).get(str(item_id), 0)
        return float(expiry) > time.time()

    def mark_seen(self, guild_id: int | str, item_id: int) -> None:
        cfg = self.get(guild_id)
        seen = [i for i in cfg.get("seen", []) if i != item_id]
        seen.append(item_id)
        cfg["seen"] = seen[-SEEN_LIMIT:]
        cfg.get("cooldowns", {}).pop(str(item_id), None)
        self.save()

    def mark_cooldown(self, guild_id: int | str, item_id: int, seconds: float = 600) -> None:
        cfg = self.get(guild_id)
        cooldowns = cfg.setdefault("cooldowns", {})
        cooldowns[str(item_id)] = time.time() + seconds
        if len(cooldowns) > 2000:
            cutoff = time.time()
            cfg["cooldowns"] = {k: v for k, v in cooldowns.items() if v > cutoff}
        self.save()

    def add_history(self, guild_id: int | str, entry: dict[str, Any]) -> None:
        cfg = self.get(guild_id)
        history = cfg.setdefault("history", [])
        entry = {"ts": time.time(), **entry}
        history.append(entry)
        cfg["history"] = history[-HISTORY_LIMIT:]
        self.save()
