"""Discord bot front-end for the Roblox free-item sniper.

Run with:  python bot.py

Two modes:

* **normal** — scan catalog pages on a timer, check ownership, then buy.
* **fast**   — purchase-first polling of the watch list. Cached item metadata lets
  the hot loop go straight to the purchase POST, skipping the catalog GET and the
  ownership GET entirely (~2 round trips saved per attempt).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings
from roblox_api import PurchaseResult, RobloxClient, RobloxError
from store import Store

log = logging.getLogger("sniper")

MIN_INTERVAL = 3.0
MIN_HOT_INTERVAL = 0.6
CATALOG_URL = "https://www.roblox.com/catalog/{item_id}"

CATEGORIES = {
    "all": 1,
    "collectibles": 2,
    "clothing": 3,
    "body": 4,
    "gear": 5,
    "accessories": 11,
    "animations": 12,
    "community": 13,
}
SORTS = {"relevance": 0, "favorited": 1, "sales": 2, "updated": 3, "price_low": 4, "price_high": 5}

# Failures that just mean "not yet" — not worth persisting to disk or showing a flag.
TRANSIENT_HINTS = (
    "expected price",
    "price mismatch",
    "not for sale",
    "not currently for sale",
    "sold out",
    "no longer available",
    "purchase is not",
    "too many requests",
)


def fmt_ts(ts: float) -> str:
    return discord.utils.format_dt(datetime.fromtimestamp(ts, tz=timezone.utc), "R")


def _short(value: Any, limit: int = 300) -> str:
    """Trim an error string so it fits inside a single embed field."""
    text = str(value or "unknown error").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _already_owned(message: str) -> bool:
    low = message.lower()
    return "already own" in low or "already purchased" in low


def _is_transient(message: str) -> bool:
    low = message.lower()
    return any(hint in low for hint in TRANSIENT_HINTS)


class SniperBot(commands.Bot):
    def __init__(self, settings: Settings, store: Store, roblox: RobloxClient) -> None:
        super().__init__(command_prefix="!snipe ", intents=discord.Intents.default(), help_command=None)
        self.settings = settings
        self.store = store
        self.roblox = roblox
        self.workers: dict[int, asyncio.Task[None]] = {}
        self.started_at = time.time()
        self._stats = {"cycles": 0, "scanned": 0, "bought": 0, "errors": 0, "hot_polls": 0}
        self._auth_task: asyncio.Task[None] | None = None
        self.sweeps: dict[int, asyncio.Task[None]] = {}
        self._sweep_notified: dict[int, int] = {}
        self._inflight = asyncio.Semaphore(max(1, int(settings.max_in_flight)))
        # Cooldowns live in memory: a fast poll loop must not rewrite state.json
        # every second just to remember "not free yet".
        self.cooldowns: dict[tuple[int, int], float] = {}

    # ------------------------------------------------------------- lifecycle
    async def setup_hook(self) -> None:
        await self.roblox.start()
        # Authenticate in the background: a slow or unreachable Roblox must never
        # hold up the Discord login (the probe can retry for a while).
        self._auth_task = asyncio.create_task(self._authenticate(), name="roblox-auth")
        await self.add_cog(SnipeCommands(self))
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced slash commands to guild %s", self.settings.guild_id)
        else:
            await self.tree.sync()
            log.info("Synced global slash commands (may take up to an hour to appear)")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """First-run guidance, so a new server isn't staring at a bare command list."""
        channel = guild.system_channel
        if channel is None or not channel.permissions_for(guild.me).send_messages:
            channel = next(
                (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages),
                None,
            )
        if channel is None:
            return
        embed = discord.Embed(
            title="👋 Roblox sniper is here",
            description=(
                "Three commands to get going:\n\n"
                "**1.** `/snipe doctor` — checks your Roblox login before anything else\n"
                "**2.** `/snipe scan` — shows what free items it can currently see\n"
                "**3.** `/snipe start` — starts watching, or `/snipe sweep start` to buy every free item\n\n"
                "Full list: `/snipe help`"
            ),
            color=0x5865F2,
        )
        embed.set_footer(text="All commands need the Manage Server permission")
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _authenticate(self) -> None:
        try:
            await self.roblox.authenticate()
        except RobloxError as exc:
            # Keep running so the slash commands still work while the cookie is
            # refreshed — `/snipe doctor` is how the user diagnoses it.
            log.error("Roblox authentication failed: %s", exc)
            log.error("Fix ROBLOX_COOKIE in .env — purchases will fail until you do.")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected error during Roblox authentication")

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, self.user.id if self.user else "?")
        for guild_id, cfg in self.store.all_guilds().items():
            if cfg.get("enabled"):
                self.start_worker(int(guild_id))

    async def close(self) -> None:
        auth_task = getattr(self, "_auth_task", None)
        if auth_task and not auth_task.done():
            auth_task.cancel()
        for task in list(self.workers.values()) + list(self.sweeps.values()):
            the_task = task
            the_task.cancel()
        await asyncio.gather(
            *self.workers.values(), *self.sweeps.values(), return_exceptions=True
        )
        await self.roblox.close()
        await super().close()

    # --------------------------------------------------------------- workers
    def worker_running(self, guild_id: int) -> bool:
        task = self.workers.get(guild_id)
        return task is not None and not task.done()

    def start_worker(self, guild_id: int) -> None:
        if self.worker_running(guild_id):
            return
        self.workers[guild_id] = asyncio.create_task(
            self._worker(guild_id), name=f"sniper-{guild_id}"
        )

    async def stop_worker(self, guild_id: int) -> None:
        task = self.workers.pop(guild_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _worker(self, guild_id: int) -> None:
        # A purchase needs the account id, so don't poll before auth resolves.
        auth_task = getattr(self, "_auth_task", None)
        if auth_task and not auth_task.done():
            log.info("Worker for guild %s waiting for Roblox authentication…", guild_id)
            try:
                await auth_task
            except (asyncio.CancelledError, Exception):
                pass
        log.info("Worker started for guild %s", guild_id)
        try:
            while True:
                cfg = self.store.get(guild_id)
                if not cfg.get("enabled"):
                    log.info("Worker for guild %s stopped (disabled)", guild_id)
                    return
                try:
                    await self.cycle(guild_id)
                except asyncio.CancelledError:
                    raise
                except RobloxError as exc:
                    self._stats["errors"] += 1
                    log.warning("Roblox error in cycle: %s", exc)
                except Exception:
                    self._stats["errors"] += 1
                    log.exception("Unhandled error in snipe cycle")

                cfg = self.store.get(guild_id)
                hot_ready = bool(cfg.get("fast")) and bool(cfg.get("watch_meta"))
                if hot_ready:
                    interval = max(MIN_HOT_INTERVAL, float(cfg.get("hot_interval") or 1.0))
                    # Tight loop, tiny jitter so polls don't line up with Roblox's
                    # own tick.
                    await asyncio.sleep(interval + random.uniform(0, 0.15))
                else:
                    interval = max(MIN_INTERVAL, float(cfg.get("interval") or 8.0))
                    await asyncio.sleep(interval + random.uniform(0, min(2.0, interval * 0.25)))
        finally:
            log.info("Worker for guild %s exiting", guild_id)
            if self.workers.get(guild_id) is asyncio.current_task():
                self.workers.pop(guild_id, None)

    # ------------------------------------------------------------- cooldowns
    def on_cooldown(self, guild_id: int, item_id: int) -> bool:
        return self.cooldowns.get((guild_id, item_id), 0.0) > time.time()

    def cool(self, guild_id: int, item_id: int, seconds: float) -> None:
        if seconds <= 0:
            self.cooldowns.pop((guild_id, item_id), None)
        else:
            self.cooldowns[(guild_id, item_id)] = time.time() + seconds
        if len(self.cooldowns) > 4000:
            now = time.time()
            self.cooldowns = {k: v for k, v in self.cooldowns.items() if v > now}

    def failure_cooldown(self, cfg: dict[str, Any], message: str) -> float:
        low = message.lower()
        if "price" in low or "expected" in low:
            return 0.25  # repriced/dropped mid-flight: retry almost immediately
        return 1.0 if cfg.get("fast") else 300.0

    # ----------------------------------------------------------------- cycle
    def should_take(self, item: dict[str, Any], cfg: dict[str, Any]) -> bool:
        if not RobloxClient.is_free(item):
            return False
        if not RobloxClient.is_purchasable(item):
            return False

        allowed = cfg.get("asset_types") or []
        asset_type = item.get("assetType")
        if allowed and asset_type not in allowed and item.get("itemType") != "Bundle":
            return False

        name = str(item.get("name") or "").lower()
        for keyword in cfg.get("exclude_keywords") or []:
            if keyword and keyword.lower() in name:
                return False
        return True

    async def _catalog_pages(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Catalog cursors are a linked list, so pages must be fetched in order."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(max(1, min(5, int(cfg.get("pages") or 1)))):
            page, cursor = await self.roblox.search_items(
                category=int(cfg["category"]),
                subcategory=cfg.get("subcategory"),
                keyword=cfg.get("keyword"),
                sort_type=int(cfg.get("sort_type") or 3),
                sales_type_filter=int(cfg.get("sales_type_filter") or 1),
                cursor=cursor,
            )
            out.extend(page)
            if not cursor:
                break
        return out

    async def collect(self, cfg: dict[str, Any], *, skip_watch: bool = False) -> list[dict[str, Any]]:
        watch_ids = [int(i) for i in (cfg.get("watch_ids") or [])][:100]
        coros: list[Any] = []
        if watch_ids and not skip_watch:
            coros.append(self.roblox.item_details(watch_ids))
        if cfg.get("category"):
            coros.append(self._catalog_pages(cfg))
        if not coros:
            return []

        found: dict[int, dict[str, Any]] = {}
        # Watch list and catalog scan run side by side instead of back to back.
        for res in await asyncio.gather(*coros, return_exceptions=True):
            if isinstance(res, BaseException):
                self._stats["errors"] += 1
                log.warning("collect() partial failure: %s", res)
                continue
            for item in res:
                if isinstance(item.get("id"), int):
                    found[item["id"]] = item
        return list(found.values())

    async def cycle(self, guild_id: int) -> dict[str, Any]:
        cfg = self.store.get(guild_id)
        summary: dict[str, Any] = {
            "scanned": 0,
            "free": 0,
            "attempted": 0,
            "bought": 0,
            "candidates": [],
            "results": [],
        }
        self._stats["cycles"] += 1
        results: list[tuple[dict[str, Any], PurchaseResult]] = []

        # Fast path: cached metadata -> purchase POST, no reads in between.
        if cfg.get("fast"):
            results.extend(await self._hot_watch(guild_id, cfg))
            self._stats["hot_polls"] += 1

        handled = {int(item["id"]) for item, _ in results}
        items = await self.collect(cfg, skip_watch=bool(cfg.get("fast")))
        summary["scanned"] = len(items)
        self._stats["scanned"] += len(items)

        candidates = [
            item for item in items
            if int(item["id"]) not in handled and self.should_take(item, cfg)
        ]
        summary["free"] = len(candidates)
        summary["candidates"] = candidates

        budget = max(1, int(cfg.get("max_purchases_per_cycle") or 3))
        picked = [
            item for item in candidates
            if not self.store.is_seen(guild_id, int(item["id"]))
            and not self.on_cooldown(guild_id, int(item["id"]))
        ][:budget]

        if picked:
            # Candidates are independent — buy them in parallel, not one by one.
            for res in await asyncio.gather(
                *[self._attempt(guild_id, cfg, item) for item in picked],
                return_exceptions=True,
            ):
                if isinstance(res, BaseException):
                    self._stats["errors"] += 1
                    log.warning("purchase task failed: %s", res)
                    continue
                if res:
                    results.append(res)

        summary["results"] = results
        summary["bought"] = sum(1 for _, r in results if r.ok)
        summary["attempted"] = sum(
            1 for _, r in results if not r.message.startswith("dry-run")
        )
        return summary

    # ------------------------------------------------------- hot (fast) path
    async def _hot_watch(self, guild_id: int, cfg: dict[str, Any]) -> list[tuple[dict[str, Any], PurchaseResult]]:
        metas = cfg.get("watch_meta") or {}
        jobs = []
        for item_id in [int(i) for i in (cfg.get("watch_ids") or [])][:20]:
            meta = metas.get(str(item_id))
            if not meta:
                continue
            if self.store.is_seen(guild_id, item_id) or self.on_cooldown(guild_id, item_id):
                continue
            jobs.append(self._hot_one(guild_id, cfg, meta))
        if not jobs:
            return []

        out: list[tuple[dict[str, Any], PurchaseResult]] = []
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, BaseException):
                self._stats["errors"] += 1
                log.warning("hot purchase task failed: %s", res)
                continue
            if res:
                out.append(res)
        return out

    async def _hot_one(self, guild_id: int, cfg: dict[str, Any], meta: dict[str, Any]):
        item = dict(meta)
        item_id = int(item["id"])

        if self.settings.dry_run:
            self.cool(guild_id, item_id, 600)
            return await self._record(guild_id, cfg, item, PurchaseResult(False, "dry-run: would have bought"))

        async with self._inflight:
            result = await self.roblox.purchase(item, hot=True)

        if result.ok or _already_owned(result.message):
            return await self._record(guild_id, cfg, item, result)

        # Cached metadata can go stale (repriced, reissued product id). One refresh,
        # and only retry if the fresh copy actually looks free — never a blind loop.
        new_meta: dict[str, Any] | None = None
        try:
            fresh = await self.roblox.item_details([item_id])
            if fresh:
                new_meta = self._meta_from_item(fresh[0])
        except RobloxError as exc:
            log.debug("Metadata refresh failed for %s: %s", item_id, exc)

        if new_meta:
            changed = any(
                new_meta.get(key) != meta.get(key)
                for key in ("productId", "collectibleItemId", "collectibleProductId", "creatorTargetId", "price")
            )
            if changed:
                self._store_watch_meta(guild_id, new_meta)
            if RobloxClient.is_free(new_meta) and RobloxClient.is_purchasable(new_meta):
                async with self._inflight:
                    result = await self.roblox.purchase(new_meta, hot=True)
                item = new_meta

        return await self._record(guild_id, cfg, item, result)

    async def _attempt(self, guild_id: int, cfg: dict[str, Any], item: dict[str, Any]):
        item_id = int(item["id"])
        if self.store.is_seen(guild_id, item_id) or self.on_cooldown(guild_id, item_id):
            return None

        if self.settings.dry_run:
            self.cool(guild_id, item_id, 600)
            return await self._record(guild_id, cfg, item, PurchaseResult(False, "dry-run: would have bought"))

        async with self._inflight:
            # The ownership GET costs a full round trip. Fast mode skips it and lets
            # "already own" come back from the purchase itself.
            if not cfg.get("fast"):
                owned = await self.roblox.is_owned(
                    item_id, item.get("assetType") or item.get("itemType") or 1
                )
                if owned:
                    self.store.mark_seen(guild_id, item_id)
                    log.debug("Already owned: %s", item_id)
                    return None
            result = await self.roblox.purchase(item, hot=True)

        return await self._record(guild_id, cfg, item, result)

    async def _record(
        self,
        guild_id: int,
        cfg: dict[str, Any],
        item: dict[str, Any],
        result: PurchaseResult,
        *,
        notify: bool = True,
    ) -> tuple[dict[str, Any], PurchaseResult]:
        item_id = int(item["id"])

        if result.ok:
            self._stats["bought"] += 1
            self.store.mark_seen(guild_id, item_id)
            self.cool(guild_id, item_id, 0)
        elif _already_owned(result.message):
            self.store.mark_seen(guild_id, item_id)
        else:
            self.cool(guild_id, item_id, self.failure_cooldown(cfg, result.message))

        # Persist only what matters — successes and real errors, not every
        # "not free yet" probe in a 1s loop.
        if result.ok or not _is_transient(result.message):
            self.store.add_history(
                guild_id,
                {
                    "item_id": item_id,
                    "name": item.get("name"),
                    "ok": result.ok,
                    "message": result.message,
                },
            )
            if not result.ok:
                log.warning("Purchase failed for %s: %s", item_id, result.message)

        if notify:
            await self.notify(guild_id, cfg, item, result)
        return (item, result)

    # ------------------------------------------------------------ bulk grab
    @staticmethod
    def strictly_free(item: dict[str, Any]) -> bool:
        """True only when the price is *verified* zero right now.

        Deliberately stricter than `is_free`: a missing price proves nothing, and
        a bulk buy must never spend Robux by accident.
        """
        try:
            price = int(item["price"]) if item.get("price") is not None else None
        except (TypeError, ValueError):
            price = None
        status = str(item.get("priceStatus") or "").strip().lower()
        if price is not None and price > 0:
            return False
        if price == 0:
            return True
        return price is None and status == "free"

    async def grab_free(self, guild_id: int, pages: int = 3, limit: int = 10) -> dict[str, Any]:
        """Find free catalog items and buy them, re-checking every price first."""
        cfg = dict(self.store.get(guild_id))
        cfg["pages"] = max(1, min(5, int(pages)))
        limit = max(1, min(50, int(limit)))

        summary: dict[str, Any] = {
            "scanned": 0, "matched": 0, "verified": 0, "bought": 0,
            "owned": 0, "skipped": 0, "results": [], "dry": self.settings.dry_run,
        }

        items = await self.collect(cfg)
        summary["scanned"] = len(items)

        candidates = [
            item for item in items
            if self.should_take(item, cfg) and not self.store.is_seen(guild_id, int(item["id"]))
        ]
        summary["matched"] = len(candidates)
        if not candidates:
            return summary

        ids = [int(item["id"]) for item in candidates][:100]
        summary["skipped"] = max(0, len(candidates) - 100)

        # Search results can be seconds stale. Re-read price straight from the
        # catalog before any money is involved.
        fresh: dict[int, dict[str, Any]] = {}
        try:
            for item in await self.roblox.item_details(ids):
                if isinstance(item.get("id"), int):
                    fresh[int(item["id"])] = item
        except RobloxError as exc:
            log.warning("grab: price verification failed: %s", exc)
            return summary

        verified = [
            fresh[i] for i in ids if i in fresh and self.strictly_free(fresh[i])
        ]
        summary["verified"] = len(verified)

        for item in verified[:limit]:
            item_id = int(item["id"])
            try:
                owned = await self.roblox.is_owned(
                    item_id, item.get("assetType") or item.get("itemType") or 1
                )
            except RobloxError:
                owned = None
            if owned:
                self.store.mark_seen(guild_id, item_id)
                summary["owned"] += 1
                continue

            if self.settings.dry_run:
                summary["results"].append(
                    (item, PurchaseResult(False, "dry-run: would have bought"))
                )
                continue

            result = await self.roblox.purchase(item, hot=True)
            await self._record(guild_id, cfg, item, result, notify=False)
            if result.ok:
                summary["bought"] += 1
            summary["results"].append((item, result))

        return summary

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _meta_from_item(item: dict[str, Any]) -> dict[str, Any]:
        """The minimum needed to POST a purchase, cached for the hot path."""
        return {
            "id": int(item["id"]),
            "name": item.get("name"),
            "price": item.get("price"),
            "lowestPrice": item.get("lowestPrice"),
            "priceStatus": item.get("priceStatus"),
            "purchaseable": item.get("purchaseable"),
            "assetType": item.get("assetType"),
            "itemType": item.get("itemType"),
            "creatorTargetId": item.get("creatorTargetId"),
            "creatorName": item.get("creatorName"),
            "productId": item.get("productId"),
            "collectibleItemId": item.get("collectibleItemId"),
            "collectibleProductId": item.get("collectibleProductId"),
            "unitsAvailableForConsumption": item.get("unitsAvailableForConsumption"),
            "totalQuantity": item.get("totalQuantity"),
        }

    def _store_watch_meta(self, guild_id: int, meta: dict[str, Any]) -> None:
        cfg = self.store.get(guild_id)
        metas = dict(cfg.get("watch_meta") or {})
        metas[str(meta["id"])] = meta
        watched = {str(i) for i in (cfg.get("watch_ids") or [])}
        self.store.update(
            guild_id, watch_meta={k: v for k, v in metas.items() if k in watched}
        )

    async def cache_watch_meta(self, guild_id: int, item_ids: list[int] | None = None) -> int:
        """Warm the fast path: cache product/seller fields for watched items."""
        cfg = self.store.get(guild_id)
        ids = [int(i) for i in (item_ids if item_ids is not None else cfg.get("watch_ids") or [])][:100]
        if not ids:
            return 0
        try:
            details = await self.roblox.item_details(ids)
        except RobloxError as exc:
            log.warning("cache_watch_meta failed: %s", exc)
            return 0
        metas = dict(cfg.get("watch_meta") or {})
        for item in details:
            if isinstance(item.get("id"), int):
                meta = self._meta_from_item(item)
                metas[str(meta["id"])] = meta
        self.store.update(guild_id, watch_meta=metas)
        return len(details)

    # ----------------------------------------------------------- catalog sweep
    def sweep_state(self, guild_id: int) -> dict[str, Any]:
        return dict(self.store.get(guild_id).get("sweep") or {})

    def sweep_running(self, guild_id: int) -> bool:
        task = self.sweeps.get(guild_id)
        return task is not None and not task.done()

    def _save_sweep(self, guild_id: int, state: dict[str, Any]) -> None:
        cfg = self.store.get(guild_id)
        cfg["sweep"] = state
        self.store.save()

    def start_sweep(
        self,
        guild_id: int,
        *,
        category: int = 1,
        sort_type: int = 3,
        max_purchases: int = 0,
        max_pages: int = 500,
        resume: bool = False,
    ) -> dict[str, Any]:
        if not self.roblox.user_id:
            raise RuntimeError(
                "The Roblox account isn't authenticated yet — run `/snipe doctor` first."
            )
        if self.sweep_running(guild_id):
            raise RuntimeError("A sweep is already running in this server.")

        previous = self.sweep_state(guild_id)
        keep = bool(resume) and previous.get("cursor")
        state = {
            "active": True,
            "cursor": previous.get("cursor") if keep else None,
            "category": int(category),
            "sort_type": int(sort_type),
            "max_purchases": max(0, int(max_purchases)),
            "max_pages": max(1, min(2000, int(max_pages))),
            "pages": int(previous.get("pages", 0)) if keep else 0,
            "scanned": int(previous.get("scanned", 0)) if keep else 0,
            "matched": int(previous.get("matched", 0)) if keep else 0,
            "verified": int(previous.get("verified", 0)) if keep else 0,
            "bought": int(previous.get("bought", 0)) if keep else 0,
            "owned": int(previous.get("owned", 0)) if keep else 0,
            "failed": int(previous.get("failed", 0)) if keep else 0,
            "started_at": time.time(),
            "finished_at": None,
            "reason": None,
            "last_error": None,
        }
        self._save_sweep(guild_id, state)
        self._sweep_notified[guild_id] = state["bought"]
        self.sweeps[guild_id] = asyncio.create_task(
            self._sweep_worker(guild_id), name=f"sweep-{guild_id}"
        )
        return state

    async def stop_sweep(self, guild_id: int) -> None:
        state = self.sweep_state(guild_id)
        state["active"] = False
        self._save_sweep(guild_id, state)
        task = self.sweeps.get(guild_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _sweep_worker(self, guild_id: int) -> None:
        """Walk the catalog page by page, buying verified-free items.

        Progress (including the pagination cursor) is persisted per page, so a
        restart or crash resumes instead of starting over.
        """
        log.info("Sweep started for guild %s", guild_id)
        reason = "stopped"
        try:
            while True:
                state = self.sweep_state(guild_id)
                if not state.get("active"):
                    reason = "stopped"
                    break
                if state.get("max_purchases") and state["bought"] >= state["max_purchases"]:
                    reason = "purchase cap reached"
                    break
                if state["pages"] >= state["max_pages"]:
                    reason = "page cap reached"
                    break

                cfg = self.store.get(guild_id)
                try:
                    page, cursor = await self.roblox.search_items(
                        category=int(state["category"]),
                        sort_type=int(state["sort_type"]),
                        limit=30,
                        cursor=state.get("cursor"),
                    )
                except RobloxError as exc:
                    state["last_error"] = str(exc)
                    self._save_sweep(guild_id, state)
                    await asyncio.sleep(6)
                    continue

                if not page:
                    reason = "end of catalog"
                    break

                state["pages"] += 1
                state["scanned"] += len(page)
                state["cursor"] = cursor

                candidates = [item for item in page if self.should_take(item, cfg)]
                state["matched"] += len(candidates)

                ids = [int(item["id"]) for item in candidates][:100]
                verified: dict[int, dict[str, Any]] = {}
                if ids:
                    try:
                        fresh = await self.roblox.item_details(ids)
                        verified = {
                            int(item["id"]): item
                            for item in fresh
                            if isinstance(item.get("id"), int) and self.strictly_free(item)
                        }
                    except RobloxError as exc:
                        state["last_error"] = str(exc)
                        verified = {}

                state["verified"] += len(verified)

                for item_id, item in verified.items():
                    # The cap is per purchase, not per page — check it mid-page too.
                    if state.get("max_purchases") and state["bought"] >= state["max_purchases"]:
                        break
                    if self.store.is_seen(guild_id, item_id) or self.on_cooldown(guild_id, item_id):
                        continue
                    try:
                        owned = await self.roblox.is_owned(
                            item_id, item.get("assetType") or item.get("itemType") or 1
                        )
                    except RobloxError:
                        owned = None
                    if owned:
                        self.store.mark_seen(guild_id, item_id)
                        state["owned"] += 1
                        continue
                    if self.settings.dry_run:
                        state["verified"] -= 1
                        continue

                    result = await self.roblox.purchase(item, hot=True)
                    await self._record(guild_id, cfg, item, result, notify=False)
                    if result.ok:
                        state["bought"] += 1
                    else:
                        state["failed"] += 1

                self._save_sweep(guild_id, state)
                await self._sweep_notify(guild_id, state)

                if not cursor:
                    reason = "end of catalog"
                    break
        except asyncio.CancelledError:
            reason = "stopped"
            raise
        except Exception as exc:
            reason = "crashed"
            log.exception("Sweep crashed")
            state = self.sweep_state(guild_id)
            state["last_error"] = str(exc)
            self._save_sweep(guild_id, state)
        finally:
            self.sweeps.pop(guild_id, None)
            state = self.sweep_state(guild_id)
            state.update(active=False, finished_at=time.time(), reason=reason)
            self._save_sweep(guild_id, state)
            log.info("Sweep finished for guild %s (%s)", guild_id, reason)
            if reason != "stopped":
                try:
                    await self._sweep_notify(guild_id, state, force=True)
                except Exception:
                    pass

    async def _sweep_notify(self, guild_id: int, state: dict[str, Any], force: bool = False) -> None:
        """Post progress every 10 purchases, or when forced (finish/stop)."""
        cfg = self.store.get(guild_id)
        channel_id = cfg.get("channel_id")
        if not channel_id:
            return
        bought = int(state.get("bought", 0))
        if not force and bought - self._sweep_notified.get(guild_id, 0) < 10:
            return
        self._sweep_notified[guild_id] = bought

        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not isinstance(channel, discord.abc.Messageable):
            return

        elapsed = max(1.0, time.time() - float(state.get("started_at") or time.time()))
        pages_per_min = state["pages"] / (elapsed / 60)
        finished = not state.get("active")
        embed = discord.Embed(
            title=("✅ Sweep finished" if finished else "🧹 Sweep running")
            + (f" — {state['reason']}" if finished and state.get("reason") else ""),
            color=0x2ECC71 if finished else 0x5865F2,
        )
        embed.add_field(name="Bought", value=f"`{bought}`", inline=True)
        embed.add_field(name="Already owned", value=f"`{state['owned']}`", inline=True)
        embed.add_field(name="Failed", value=f"`{state['failed']}`", inline=True)
        embed.add_field(
            name="Scanned",
            value=f"`{state['scanned']}` items over `{state['pages']}` pages "
            f"(`{pages_per_min:.1f}` pages/min)",
            inline=False,
        )
        embed.add_field(
            name="Price-verified free",
            value=f"`{state['verified']}`",
            inline=True,
        )
        if state.get("last_error"):
            embed.add_field(name="Last error", value=str(state["last_error"])[:300], inline=False)
        embed.set_footer(
            text=("Stopped" if finished else "Use /snipe sweep stop to halt") + " · every buy checked price == 0"
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------ notifying
    async def notify(
        self,
        guild_id: int,
        cfg: dict[str, Any],
        item: dict[str, Any],
        result: PurchaseResult,
    ) -> None:
        channel_id = cfg.get("channel_id")
        if not channel_id:
            return
        channel = self.get_channel(int(channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(channel_id))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if not isinstance(channel, discord.abc.Messageable):
            return

        item_id = int(item["id"])
        price = int(item.get("price") or item.get("lowestPrice") or 0)
        embed = discord.Embed(
            title=("🎯 Got it — " if result.ok else "❌ Failed — ") + str(item.get("name") or f"Item {item_id}"),
            url=CATALOG_URL.format(item_id=item_id),
            color=0x2ECC71 if result.ok else 0xE74C3C,
        )
        embed.add_field(name="Price", value=f"R${price}" + (" (free)" if price == 0 else ""), inline=True)
        embed.add_field(name="Item ID", value=str(item_id), inline=True)
        embed.add_field(name="Status", value=result.message[:1024], inline=False)
        if item.get("creatorName"):
            embed.add_field(name="Creator", value=str(item["creatorName"]), inline=True)
        embed.set_footer(text="Roblox sniper")
        # Static thumbnail URL — no extra API round trip in the purchase path.
        embed.set_thumbnail(
            url=f"https://www.roblox.com/asset-thumbnail/image?assetId={item_id}&width=420&height=420&format=png"
        )
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Could not send notification: %s", exc)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def _is_manager(interaction: discord.Interaction) -> bool:
    perms = interaction.permissions
    return bool(perms.manage_guild or perms.administrator)


async def _manager_check(interaction: discord.Interaction) -> bool:
    if interaction.guild_id is None:
        raise app_commands.CheckFailure("Use this inside a server.")
    if not _is_manager(interaction):
        raise app_commands.CheckFailure("You need the **Manage Server** permission.")
    return True


manager_only = app_commands.check(_manager_check)


class SnipeCommands(commands.Cog):
    def __init__(self, bot: SniperBot) -> None:
        self.bot = bot
        self.store = bot.store
        self.settings = bot.settings

    snipe = app_commands.Group(
        name="snipe",
        description="Roblox free-item sniper",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    # ------------------------------------------------------------------ start
    @snipe.command(name="start", description="Start sniping free items into this channel")
    @manager_only
    @app_commands.describe(
        interval="Seconds between catalog scans (min 3, default 8)",
        pages="How many catalog pages to scan each cycle (1-5)",
        max_per_cycle="How many purchases to attempt per scan (1-5)",
    )
    async def start(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        interval: float = 8.0,
        pages: int = 2,
        max_per_cycle: int = 3,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = self.store.update(
            interaction.guild_id,
            enabled=True,
            channel_id=(channel or interaction.channel).id,
            interval=max(MIN_INTERVAL, float(interval)),
            pages=max(1, min(5, int(pages))),
            max_purchases_per_cycle=max(1, min(5, int(max_per_cycle))),
        )
        # Pre-open the TLS connection, cache a CSRF token and refresh watched
        # metadata so the first live poll is already warm.
        warm = 0
        if cfg.get("watch_ids"):
            warm = await self.bot.cache_watch_meta(interaction.guild_id)
        await self.bot.roblox.warmup(cfg.get("watch_ids") or [])
        self.bot.start_worker(interaction.guild_id)

        mode = "DRY RUN (nothing is actually bought)" if self.settings.dry_run else "LIVE"
        hot = cfg.get("fast") and warm
        await interaction.followup.send(
            f"✅ Sniper started in **{mode}** mode.\n"
            f"• Mode: {'⚡ **fast** — purchase-first polling every ' + str(cfg['hot_interval']) + 's' if hot else '🐢 normal — catalog scans every ' + str(int(cfg['interval'])) + 's'}\n"
            f"• Notifications → <#{cfg['channel_id']}>\n"
            f"• Category `{cfg['category']}`, sort `{cfg['sort_type']}`"
            + (f", keyword `{cfg['keyword']}`" if cfg.get("keyword") else "")
            + f"\n• Watch list: {len(cfg.get('watch_ids') or [])} item(s), `{warm}` cached for the hot path",
            ephemeral=True,
        )

    @snipe.command(name="stop", description="Stop the sniper in this server")
    @manager_only
    async def stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        self.store.update(interaction.guild_id, enabled=False)
        await self.bot.stop_worker(interaction.guild_id)
        await interaction.followup.send("🛑 Sniper stopped.", ephemeral=True)

    # -------------------------------------------------------------- fast mode
    @snipe.command(
        name="fast",
        description="Turbo mode: buy straight from cached metadata, poll watched items every ~1s",
    )
    @manager_only
    @app_commands.describe(
        mode="on or off",
        interval="Hot poll interval in seconds (0.6-5, default 1.0)",
    )
    async def fast(
        self,
        interaction: discord.Interaction,
        mode: str = "on",
        interval: float = 1.0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        value = mode.strip().lower() in ("on", "true", "yes", "1", "enable", "enabled")
        hot_interval = max(MIN_HOT_INTERVAL, min(5.0, float(interval)))
        cfg = self.store.update(interaction.guild_id, fast=value, hot_interval=hot_interval)

        if not value:
            await interaction.followup.send(
                "🐢 Fast mode **off** — back to catalog scanning every "
                f"`{int(cfg['interval'])}s`.",
                ephemeral=True,
            )
            return

        cached = await self.bot.cache_watch_meta(interaction.guild_id)
        await self.bot.roblox.warmup(cfg.get("watch_ids") or [])
        if cfg.get("enabled"):
            self.bot.start_worker(interaction.guild_id)

        if cached == 0:
            await interaction.followup.send(
                "⚡ Fast mode **on**, but there is nothing on the watch list yet.\n"
                "Add the drop with `/snipe watch <item id>` — fast mode only pays off for "
                "known items, so it falls back to catalog scanning until you do.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"⚡ Fast mode **on** — purchase-first polling every `{hot_interval:g}s`.\n"
            f"• `{cached}` item(s) cached: the poll goes straight to the purchase POST "
            "(no catalog read, no ownership check)\n"
            f"• For minimum latency run `/snipe filter category:none` so no scan traffic "
            "shares the connection\n"
            f"• Requests pace at `{self.settings.hot_gap:g}s` — that is the floor, "
            "not a suggestion. Lowering it mostly buys 429s.",
            ephemeral=True,
        )

    # ----------------------------------------------------------------- status
    @snipe.command(name="status", description="Show the sniper's current configuration and health")
    @manager_only
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = self.store.get(interaction.guild_id)
        running = self.bot.worker_running(interaction.guild_id)
        balance = await self.bot.roblox.robux_balance()
        hot = bool(cfg.get("fast")) and bool(cfg.get("watch_meta"))

        embed = discord.Embed(title="🤖 Sniper status", color=0x5865F2)
        embed.add_field(
            name="State",
            value=("🟢 running" if running else "⚪ idle") + (" (dry run)" if self.settings.dry_run else ""),
            inline=True,
        )
        embed.add_field(
            name="Account",
            value=f"{self.bot.roblox.username or '?'} (`{self.bot.roblox.user_id}`)",
            inline=True,
        )
        embed.add_field(name="Robux", value=str(balance) if balance is not None else "unknown", inline=True)
        embed.add_field(
            name="Mode",
            value=(
                f"⚡ fast — `{float(cfg.get('hot_interval') or 1):g}s` hot polls, "
                f"`{len(cfg.get('watch_meta') or {})}` cached"
                if hot
                else f"🐢 normal — `{int(cfg['interval'])}s` catalog scans"
            ),
            inline=True,
        )
        embed.add_field(name="Last request", value=f"`{self.bot.roblox.last_latency_ms} ms`", inline=True)
        embed.add_field(
            name="Scanner",
            value=f"`{cfg['pages']}` page(s) · cat `{cfg['category']}`"
            + (f" · sub `{cfg['subcategory']}`" if cfg.get("subcategory") else "")
            + f" · sort `{cfg['sort_type']}`",
            inline=False,
        )
        filters = []
        if cfg.get("keyword"):
            filters.append(f"keyword=`{cfg['keyword']}`")
        if cfg.get("exclude_keywords"):
            filters.append("exclude=" + ", ".join(cfg["exclude_keywords"]))
        if cfg.get("asset_types"):
            filters.append("assetTypes=" + ", ".join(str(a) for a in cfg["asset_types"]))
        embed.add_field(name="Filters", value="\n".join(filters) or "none (any free item)", inline=False)
        embed.add_field(name="Watch list", value=str(len(cfg.get("watch_ids") or [])) + " item(s)", inline=True)
        embed.add_field(name="Handled items", value=f"`{len(cfg.get('seen') or [])}`", inline=True)

        runtime = int(time.time() - self.bot.started_at)
        embed.set_footer(
            text=f"cycles {self.bot._stats['cycles']} · seen {self.bot._stats['scanned']} · "
            f"bought {self.bot._stats['bought']} · errors {self.bot._stats['errors']} · up {runtime // 60}m"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------------------------------------------------------------- doctor
    @snipe.command(
        name="doctor",
        description="Check the Roblox cookie: login, CSRF handshake, Robux balance, catalog access",
    )
    @manager_only
    async def doctor(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        report = await self.bot.roblox.diagnose()

        if report["cookie_present"]:
            cookie_line = f"✅ present — {report['cookie_length']:,} chars"
            if not report["cookie_prefixed"]:
                cookie_line += (
                    "\n⚠️ doesn't start with `_|WARNING` — usually a truncated or partial copy"
                )
        else:
            cookie_line = "❌ missing from the environment"

        if report["auth_ok"]:
            login_line = f"✅ **{report['username']}** (id `{report['user_id']}`) — {report['auth_ms']} ms"
        else:
            login_line = f"❌ {_short(report['auth_error'])} — {report['auth_ms']} ms"

        csrf_line = (
            f"✅ token acquired — {report['csrf_ms']} ms"
            if report["csrf_ok"]
            else f"❌ {_short(report['csrf_error'])} — {report['csrf_ms']} ms"
        )

        if report["robux"] is not None:
            robux_line = f"✅ {report['robux']:,} Robux"
        elif report["robux_error"]:
            robux_line = f"❌ {_short(report['robux_error'])}"
        else:
            robux_line = "⚠️ unknown"

        catalog_line = (
            f"✅ {report['catalog_items']} items readable"
            if report["catalog_ok"]
            else f"❌ {_short(report['catalog_error'])}"
        )

        if not report["auth_ok"]:
            verdict = (
                "**Cookie is dead.** Log into Roblox in a private window and copy a fresh "
                "`.ROBLOSECURITY` value — a password change or \"log out of all sessions\" kills it."
            )
            color = 0xE74C3C
        elif not report["csrf_ok"]:
            verdict = (
                "**Logged in, but POSTs are refused.** The session is only partly valid, so purchases "
                "will fail. Re-copy the cookie; if it persists, check that the account isn't restricted."
            )
            color = 0xE67E22
        elif not report["catalog_ok"]:
            verdict = (
                "**Session is valid but the catalog is unreadable** — usually a Roblox-side hiccup "
                "or a rate limit. Retry in a minute."
            )
            color = 0xE67E22
        else:
            verdict = (
                "**Everything checks out.** Purchases should go through — run `/snipe scan` to "
                "confirm the filters before going live."
            )
            color = 0x2ECC71

        embed = discord.Embed(title="🩺 Roblox connection doctor", color=color)
        embed.add_field(name="Cookie", value=cookie_line, inline=False)
        embed.add_field(name="Login", value=login_line, inline=False)
        embed.add_field(name="CSRF handshake", value=csrf_line, inline=False)
        embed.add_field(name="Robux balance", value=robux_line, inline=False)
        embed.add_field(name="Catalog read", value=catalog_line, inline=False)
        embed.add_field(name="Verdict", value=verdict, inline=False)
        if self.settings.dry_run:
            embed.set_footer(text="DRY_RUN is enabled — the sniper will not actually buy")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------- scan
    @snipe.command(name="scan", description="Run one scan and show what it would grab (never buys)")
    @manager_only
    async def scan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = self.store.get(interaction.guild_id)
        items = await self.bot.collect(cfg)
        candidates = [i for i in items if self.bot.should_take(i, cfg)]

        if not candidates:
            await interaction.followup.send(
                f"Scanned **{len(items)}** items — no free ones matched your filters right now.",
                ephemeral=True,
            )
            return

        lines = []
        for item in candidates[:15]:
            price = item.get("price") or item.get("lowestPrice") or 0
            lines.append(
                f"• [{item.get('name')}]({CATALOG_URL.format(item_id=item['id'])}) — "
                f"R${price} · id `{item['id']}`"
            )
        embed = discord.Embed(
            title=f"🔍 {len(candidates)} free item(s) matched ({len(items)} scanned)",
            description="\n".join(lines),
            color=0x5865F2,
        )
        if self.settings.dry_run:
            embed.set_footer(text="DRY_RUN is enabled — /snipe start will not buy anything")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------- grab
    @snipe.command(
        name="grab",
        description="Buy every item it can verify is free right now (price re-checked before each buy)",
    )
    @manager_only
    @app_commands.describe(
        pages="Catalog pages to scan (1-5, 30 items each)",
        limit="Maximum purchases this run (1-50, default 10)",
    )
    async def grab(
        self,
        interaction: discord.Interaction,
        pages: int = 3,
        limit: int = 10,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = self.store.get(interaction.guild_id)
        if not cfg.get("category"):
            await interaction.followup.send(
                "`grab` needs catalog scanning, but **category** is currently off "
                "(`/snipe filter category:none`).\n"
                "Set one first, e.g. `/snipe filter category:all`.",
                ephemeral=True,
            )
            return

        summary = await self.bot.grab_free(interaction.guild_id, pages, limit)

        lines = []
        for item, result in summary["results"][:12]:
            icon = "✅" if result.ok else ("🧪" if result.message.startswith("dry-run") else "❌")
            lines.append(
                f"{icon} [{item.get('name')}]({CATALOG_URL.format(item_id=item['id'])}) — "
                f"{result.message[:60]}"
            )

        embed = discord.Embed(
            title="🛒 Grab free items",
            description="\n".join(lines) or "Nothing to report.",
            color=0x2ECC71 if summary["bought"] else 0x5865F2,
        )
        embed.add_field(
            name="Run",
            value=(
                f"scanned `{summary['scanned']}` · matched `{summary['matched']}` · "
                f"price-verified free `{summary['verified']}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Result",
            value=(
                f"bought `{summary['bought']}` · already owned `{summary['owned']}`"
                + (f" · over batch limit `{summary['skipped']}`" if summary["skipped"] else "")
                + (f" · capped at `{limit}`" if summary["verified"] > limit else "")
            ),
            inline=False,
        )
        if summary["dry"]:
            embed.set_footer(
                text="DRY_RUN is on — nothing was actually bought. Set DRY_RUN=false to go live."
            )
        else:
            embed.set_footer(text="Every purchase used the verified price of R$0.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- filter
    @snipe.command(name="filter", description="Change what the sniper looks for")
    @manager_only
    @app_commands.describe(
        category="Catalog category to scan (default: all)",
        sort="Sort order of the catalog scan (default: recently updated)",
        keyword="Only scan results matching this search term",
        exclude="Comma-separated words to skip, e.g. 'shirt,pants'",
        asset_types="Comma-separated asset type ids to allow, e.g. '8,41,42' (empty = any)",
    )
    async def filter(
        self,
        interaction: discord.Interaction,
        category: str | None = None,
        sort: str | None = None,
        keyword: str | None = None,
        exclude: str | None = None,
        asset_types: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        changes: dict[str, Any] = {}

        if category is not None:
            value = category.strip().lower()
            if value.isdigit():
                changes["category"] = int(value) if int(value) > 0 else None
            elif value in CATEGORIES:
                changes["category"] = CATEGORIES[value]
            elif value in ("none", "off", "watchlist"):
                changes["category"] = None
            else:
                await interaction.followup.send(
                    f"Unknown category `{category}`. Use one of: {', '.join(CATEGORIES)}", ephemeral=True
                )
                return

        if sort is not None:
            key = sort.strip().lower()
            if key.isdigit():
                changes["sort_type"] = int(key)
            elif key in SORTS:
                changes["sort_type"] = SORTS[key]
            else:
                await interaction.followup.send(
                    f"Unknown sort `{sort}`. Use one of: {', '.join(SORTS)}", ephemeral=True
                )
                return

        if keyword is not None:
            changes["keyword"] = keyword.strip() or None
        if exclude is not None:
            changes["exclude_keywords"] = [w.strip() for w in exclude.split(",") if w.strip()]
        if asset_types is not None:
            try:
                changes["asset_types"] = [int(x) for x in asset_types.split(",") if x.strip()]
            except ValueError:
                await interaction.followup.send("`asset_types` must be comma-separated numbers.", ephemeral=True)
                return

        if not changes:
            await interaction.followup.send("Nothing to change — pass at least one option.", ephemeral=True)
            return

        cfg = self.store.update(interaction.guild_id, **changes)
        summary = (
            f"Category `{cfg['category']}` · sort `{cfg['sort_type']}`"
            + (f" · keyword `{cfg['keyword']}`" if cfg.get("keyword") else "")
            + (f" · exclude {cfg['exclude_keywords']}" if cfg.get("exclude_keywords") else "")
            + (f" · assetTypes {cfg['asset_types']}" if cfg.get("asset_types") else "")
        )
        await interaction.followup.send(f"✅ Filters updated.\n{summary}", ephemeral=True)

    # ------------------------------------------------------------ watch list
    @snipe.command(name="watch", description="Always poll a specific item id (great for upcoming drops)")
    @manager_only
    @app_commands.describe(item_id="Roblox catalog item id, or a catalog URL")
    async def watch(self, interaction: discord.Interaction, item_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        parsed = self._parse_item_id(item_id)
        if parsed is None:
            await interaction.followup.send("That doesn't look like an item id or catalog URL.", ephemeral=True)
            return
        try:
            details = await self.bot.roblox.item_details([parsed])
        except RobloxError as exc:
            await interaction.followup.send(f"Roblox lookup failed: {exc}", ephemeral=True)
            return
        if not details:
            await interaction.followup.send(f"No catalog item found with id `{parsed}`.", ephemeral=True)
            return

        cfg = self.store.get(interaction.guild_id)
        watch_ids = list(cfg.get("watch_ids") or [])
        if parsed in watch_ids:
            await interaction.followup.send(f"`{parsed}` is already on the watch list.", ephemeral=True)
            return
        watch_ids.append(parsed)
        self.store.update(interaction.guild_id, watch_ids=watch_ids[-100:])
        # Cache purchase fields now so fast mode never needs a read before the POST.
        await self.bot.cache_watch_meta(interaction.guild_id, [parsed])

        item = details[0]
        if self.settings.dry_run:
            detail_line = "cached for the fast path"
        elif RobloxClient.is_free(item):
            detail_line = "FREE right now — cached for the fast path"
        else:
            price = item.get("price") or item.get("lowestPrice") or "?"
            detail_line = f"currently R${price} — cached for the fast path"
        await interaction.followup.send(
            f"👁 Watching **{item.get('name')}** (`{parsed}`) — {detail_line}.\n"
            "Run `/snipe fast on` to poll it every second with no read before the purchase.",
            ephemeral=True,
        )

    @snipe.command(name="unwatch", description="Remove an item from the watch list (all items if omitted)")
    @manager_only
    async def unwatch(self, interaction: discord.Interaction, item_id: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        if item_id is None:
            self.store.update(interaction.guild_id, watch_ids=[], watch_meta={})
            await interaction.followup.send("Watch list cleared.", ephemeral=True)
            return
        parsed = self._parse_item_id(item_id)
        if parsed is None:
            await interaction.followup.send("That doesn't look like an item id.", ephemeral=True)
            return
        cfg = self.store.get(interaction.guild_id)
        watch_ids = [i for i in (cfg.get("watch_ids") or []) if i != parsed]
        metas = {
            k: v for k, v in (cfg.get("watch_meta") or {}).items() if k != str(parsed)
        }
        self.store.update(interaction.guild_id, watch_ids=watch_ids, watch_meta=metas)
        await interaction.followup.send(f"Removed `{parsed}` from the watch list.", ephemeral=True)

    # ---------------------------------------------------------------- history
    @snipe.command(name="history", description="Recent purchase attempts in this server")
    @manager_only
    async def history(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = self.store.get(interaction.guild_id)
        entries = list(reversed(cfg.get("history") or []))[:12]
        if not entries:
            await interaction.followup.send("No purchase attempts yet.", ephemeral=True)
            return
        lines = [
            f"{'✅' if e.get('ok') else '❌'} **{e.get('name')}** (`{e.get('item_id')}`) — "
            f"{e.get('message', '')[:80]} · {fmt_ts(e['ts'])}"
            for e in entries
        ]
        embed = discord.Embed(title="Purchase history", description="\n".join(lines), color=0x5865F2)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # -------------------------------------------------------------- manual buy
    @snipe.command(name="buy", description="Manually buy one item right now (bypasses the scanner)")
    @manager_only
    @app_commands.describe(item_id="Roblox catalog item id, or a catalog URL")
    async def buy(self, interaction: discord.Interaction, item_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        parsed = self._parse_item_id(item_id)
        if parsed is None:
            await interaction.followup.send("That doesn't look like an item id or catalog URL.", ephemeral=True)
            return
        if self.settings.dry_run:
            await interaction.followup.send("DRY_RUN is enabled — refusing to buy.", ephemeral=True)
            return

        try:
            details = await self.bot.roblox.item_details([parsed])
        except RobloxError as exc:
            await interaction.followup.send(f"Roblox lookup failed: {exc}", ephemeral=True)
            return
        if not details:
            await interaction.followup.send(f"No catalog item with id `{parsed}`.", ephemeral=True)
            return

        item = details[0]
        result = await self.bot.roblox.purchase(item)
        self.store.add_history(
            interaction.guild_id,
            {"item_id": parsed, "name": item.get("name"), "ok": result.ok, "message": result.message},
        )
        prefix = "✅" if result.ok else "❌"
        await interaction.followup.send(f"{prefix} **{item.get('name')}** — {result.message}", ephemeral=True)

    # ------------------------------------------------------------- sweep group
    sweep = app_commands.Group(
        name="sweep",
        description="Buy every free item in the Roblox catalog (long-running, resumable)",
        parent=snipe,
    )

    @sweep.command(name="start", description="Start buying every free item it can reach in the catalog")
    @manager_only
    @app_commands.describe(
        category="Catalog category to walk (default: all)",
        sort="Catalog order to walk in (default: updated)",
        limit="Stop after buying this many items (0 = no cap)",
        pages="Safety cap on catalog pages this run (30 items each, default 500)",
        resume="Continue from the last saved position instead of starting over",
    )
    async def sweep_start(
        self,
        interaction: discord.Interaction,
        category: str = "all",
        sort: str = "updated",
        limit: int = 0,
        pages: int = 500,
        resume: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        cat_key = category.strip().lower()
        cat = CATEGORIES.get(cat_key, int(cat_key) if cat_key.isdigit() else 1)
        sort_key = sort.strip().lower()
        sort_type = SORTS.get(sort_key, int(sort_key) if sort_key.isdigit() else 3)

        try:
            state = self.bot.start_sweep(
                interaction.guild_id,
                category=cat,
                sort_type=sort_type,
                max_purchases=limit,
                max_pages=pages,
                resume=resume,
            )
        except RuntimeError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        total = f"`{limit}` then stop" if limit else "until the catalog ends or the page cap"
        await interaction.followup.send(
            f"🧹 **Sweep started.**\n"
            f"• Walking category `{cat}`, sorted by `{sort_type}`, buying **{total}**\n"
            f"• Every item's price is re-read before buying — only `R$0` gets purchased\n"
            f"• Progress posts to <#{self.store.get(interaction.guild_id).get('channel_id')}> "
            "every 10 purchases\n"
            + ("• Resuming from the saved cursor" if state.get("cursor") else "")
            + ("\n⚠️ `DRY_RUN` is on — this will only *report* what it would buy"
               if self.settings.dry_run else ""),
            ephemeral=True,
        )

    @sweep.command(name="status", description="Show sweep progress")
    @manager_only
    async def sweep_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        state = self.bot.sweep_state(interaction.guild_id)
        if not state.get("started_at"):
            await interaction.followup.send("No sweep has run yet. Start one with `/snipe sweep start`.", ephemeral=True)
            return

        running = self.bot.sweep_running(interaction.guild_id)
        elapsed = max(1.0, time.time() - float(state.get("started_at") or time.time()))
        embed = discord.Embed(
            title="🧹 Sweep " + ("running" if running else "stopped"),
            color=0x5865F2 if running else 0x95A5A6,
        )
        embed.add_field(name="Bought", value=f"`{state.get('bought', 0)}`", inline=True)
        embed.add_field(name="Already owned", value=f"`{state.get('owned', 0)}`", inline=True)
        embed.add_field(name="Failed", value=f"`{state.get('failed', 0)}`", inline=True)
        embed.add_field(
            name="Progress",
            value=(
                f"`{state.get('scanned', 0)}` items scanned · `{state.get('pages', 0)}` pages · "
                f"`{state.get('pages', 0) / (elapsed / 60):.1f}` pages/min"
            ),
            inline=False,
        )
        embed.add_field(
            name="Free & verified",
            value=f"`{state.get('verified', 0)}` items passed the R$0 check",
            inline=False,
        )
        if state.get("last_error"):
            embed.add_field(name="Last error", value=str(state["last_error"])[:300], inline=False)
        if not running and state.get("reason"):
            embed.add_field(name="Stopped because", value=str(state["reason"]), inline=False)
        embed.set_footer(text="Continue later with /snipe sweep start (resume: true)")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @sweep.command(name="stop", description="Stop the running sweep")
    @manager_only
    async def sweep_stop(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self.bot.sweep_running(interaction.guild_id):
            await interaction.followup.send("No sweep is running.", ephemeral=True)
            return
        await self.bot.stop_sweep(interaction.guild_id)
        state = self.bot.sweep_state(interaction.guild_id)
        await interaction.followup.send(
            f"🛑 Sweep stopped — bought `{state.get('bought', 0)}` items. "
            "Resume with `/snipe sweep start`.",
            ephemeral=True,
        )

    # ------------------------------------------------------------- lookups
    @snipe.command(name="item", description="Look up one item: price, creator, availability, ownership")
    @manager_only
    @app_commands.describe(item_id="Roblox catalog item id, or a catalog URL")
    async def item_info(self, interaction: discord.Interaction, item_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        parsed = self._parse_item_id(item_id)
        if parsed is None:
            await interaction.followup.send("That doesn't look like an item id or catalog URL.", ephemeral=True)
            return
        try:
            details = await self.bot.roblox.item_details([parsed])
        except RobloxError as exc:
            await interaction.followup.send(f"Roblox lookup failed: {exc}", ephemeral=True)
            return
        if not details:
            await interaction.followup.send(f"No catalog item with id `{parsed}`.", ephemeral=True)
            return

        item = details[0]
        price = item.get("price")
        owned = await self.bot.roblox.is_owned(
            parsed, item.get("assetType") or item.get("itemType") or 1
        )
        embed = discord.Embed(
            title=str(item.get("name") or f"Item {parsed}"),
            url=CATALOG_URL.format(item_id=parsed),
            color=0x2ECC71 if self.bot.strictly_free(item) else 0x5865F2,
        )
        embed.add_field(
            name="Price",
            value=("**free** (R$0)" if self.bot.strictly_free(item)
                   else f"R${price if price is not None else '?'}"),
            inline=True,
        )
        embed.add_field(name="Item ID", value=f"`{parsed}`", inline=True)
        embed.add_field(name="Owned", value=("yes" if owned else "no" if owned is False else "unknown"), inline=True)
        if item.get("creatorName"):
            embed.add_field(name="Creator", value=str(item["creatorName"]), inline=True)
        if item.get("assetType"):
            embed.add_field(name="Asset type", value=str(item["assetType"]), inline=True)
        if item.get("unitsAvailableForConsumption") is not None:
            embed.add_field(
                name="Stock", value=str(item["unitsAvailableForConsumption"]), inline=True
            )
        embed.set_footer(text="Free items are bought with expectedPrice 0 — never more")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @snipe.command(name="whoami", description="Which Roblox account is connected, and is it healthy")
    @manager_only
    async def whoami(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        client = self.bot.roblox
        balance = await client.robux_balance()
        embed = discord.Embed(title="Roblox account", color=0x5865F2)
        embed.add_field(
            name="Account",
            value=(f"**{client.username}** (`{client.user_id}`)" if client.user_id else "❌ not authenticated"),
            inline=False,
        )
        embed.add_field(name="Robux", value=str(balance) if balance is not None else "unknown", inline=True)
        embed.add_field(name="CSRF token", value="cached ✅" if client._csrf else "missing ❌", inline=True)
        embed.add_field(name="Last request", value=f"`{client.last_latency_ms} ms`", inline=True)
        if not client.user_id:
            embed.add_field(
                name="Fix",
                value="Set `ROBLOX_COOKIE` (or run `python connect_roblox.py`), then restart the bot.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @snipe.command(name="ping", description="Measure Discord and Roblox latency")
    @manager_only
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        started = time.perf_counter()
        ok = True
        try:
            await self.bot.roblox.item_details([1])
        except RobloxError:
            ok = False
        roblox_ms = (time.perf_counter() - started) * 1000
        ws_ms = (self.bot.latency or 0) * 1000
        await interaction.followup.send(
            f"🏓 Discord gateway `{ws_ms:.0f} ms` · Roblox API `{roblox_ms:.0f} ms`"
            + ("" if ok else " (last call failed — see `/snipe doctor`)"),
            ephemeral=True,
        )

    # ------------------------------------------------------------------- help
    @snipe.command(name="help", description="Start here — every command explained in plain English")
    @manager_only
    async def help_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = self.store.get(interaction.guild_id)
        mode = "⚡ fast" if cfg.get("fast") else "🐢 normal"
        sweep = "running" if self.bot.sweep_running(interaction.guild_id) else "idle"

        embed = discord.Embed(
            title="Roblox sniper — start here",
            description=(
                "**Three commands is all you need:**\n"
                "`1.` `/snipe doctor` — is the Roblox account actually logged in?\n"
                "`2.` `/snipe scan` — what free items can it see right now?\n"
                "`3.` `/snipe start` — watch for them · or `/snipe sweep start` to buy everything free\n\n"
                f"Mode **{mode}** · sweep **{sweep}** · buying "
                f"**{'OFF (DRY_RUN)' if self.settings.dry_run else 'LIVE'}**"
            ),
            color=0x5865F2,
        )
        embed.add_field(
            name="🛒 Get free items",
            value=(
                "`/snipe sweep start` — walk the whole catalog and buy **every** item that is "
                "free, resuming where it left off\n"
                "`/snipe sweep status` · `/snipe sweep stop`\n"
                "`/snipe grab [pages] [limit]` — one quick pass, e.g. `/snipe grab pages:5 limit:25`\n"
                "`/snipe buy <id|url>` — buy one thing right now"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎯 Aim it at a specific drop",
            value=(
                "`/snipe watch <id|url>` — always poll one item\n"
                "`/snipe filter category:collectibles keyword:\"free\" exclude:shirt,pants`\n"
                "`/snipe unwatch [id]` — drop one, or clear the list"
            ),
            inline=False,
        )
        embed.add_field(
            name="▶️ Running it",
            value=(
                "`/snipe start` — begin watching in this channel\n"
                "`/snipe fast on interval:1` — turbo mode for watched items\n"
                "`/snipe stop` — stop watching (sweeps stop separately)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔧 Checking things",
            value=(
                "`/snipe doctor` — cookie, account, Robux, CSRF, catalog\n"
                "`/snipe whoami` — which account, is it healthy\n"
                "`/snipe item <id>` — price, creator, stock, do you own it\n"
                "`/snipe status` — full configuration\n"
                "`/snipe history` — last 12 purchase attempts\n"
                "`/snipe ping` — Discord + Roblox latency\n"
                "`/snipe scan` — preview matches without buying"
            ),
            inline=False,
        )
        embed.add_field(
            name="📋 Three common recipes",
            value=(
                '**"Get every free item"** → `/snipe sweep start`\n'
                '**"Get this one drop"** → `/snipe watch <url>` then `/snipe fast on`\n'
                '**"Just show me"** → `/snipe scan` (or keep `DRY_RUN=true`)'
            ),
            inline=False,
        )
        embed.set_footer(
            text="Every command needs Manage Server · free means price R$0, re-checked before each buy"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- helper
    @staticmethod
    def _parse_item_id(raw: str) -> int | None:
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        # https://www.roblox.com/catalog/123456789/name
        parts = [p for p in raw.replace("?", "/").split("/") if p]
        for part in reversed(parts):
            if part.isdigit():
                return int(part)
        return None


def main() -> None:
    settings = Settings.load()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    store = Store(settings.data_path)
    roblox = RobloxClient(
        settings.roblox_cookie,
        min_request_gap=settings.request_gap,
        hot_gap=settings.hot_gap,
    )
    bot = SniperBot(settings, store, roblox)

    async def runner() -> None:
        async with bot:
            await bot.start(settings.discord_token)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
