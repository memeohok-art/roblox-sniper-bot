"""Offline self-test — no Discord token, no Roblox cookie, no network.

Exercises the parts that are easy to get wrong: free-item filtering, the
purchase-first fast path, stale-metadata recovery, transient-failure handling,
parallel candidates, lane independence and the doctor report.

    python selftest.py

Exit code 0 means every check passed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import Settings  # noqa: E402
from roblox_api import PurchaseResult, RobloxClient, RobloxError  # noqa: E402
from store import Store  # noqa: E402
from bot import SniperBot  # noqa: E402

# The bot's config loads .env, which may contain empty placeholders. Force
# non-empty test values so the self-test works on a freshly cloned project.
if not os.environ.get("DISCORD_TOKEN", "").strip():
    os.environ["DISCORD_TOKEN"] = "selftest"
if not os.environ.get("ROBLOX_COOKIE", "").strip():
    os.environ["ROBLOX_COOKIE"] = "_|WARNING:-DO-NOT-SHARE-SELFTEST"
os.environ["DRY_RUN"] = "false"

RTT = 0.15  # simulated round trip to Roblox, per request

PASSED: list[str] = []

FREE_META = {
    "id": 111,
    "name": "Free Limited",
    "price": 0,
    "priceStatus": "Free",
    "purchaseable": True,
    "assetType": 8,
    "creatorTargetId": 42,
    "collectibleItemId": "cid",
    "collectibleProductId": "cpid",
    "itemType": "Asset",
}
PAID_META = dict(FREE_META, price=5, priceStatus="PriceLabel")


def check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    PASSED.append(label)


def new_bot() -> SniperBot:
    settings = Settings.load()
    bot = SniperBot(
        settings,
        Store(os.path.join(tempfile.mkdtemp(), "state.json")),
        RobloxClient("selftest"),
    )
    bot.roblox.user_id = 7
    return bot


# --------------------------------------------------------------------------- #
# 1. filtering
# --------------------------------------------------------------------------- #
def test_filters() -> None:
    check(RobloxClient.is_free({"price": 0}), "price 0 is free")
    check(RobloxClient.is_free({"lowestPrice": 0}), "lowestPrice 0 is free")
    check(RobloxClient.is_free({"priceStatus": "Free"}), "priceStatus Free is free")
    check(not RobloxClient.is_free({"price": 500}), "priced item is not free")
    check(not RobloxClient.is_purchasable({"purchaseable": False}), "unpurchasable is rejected")
    check(
        not RobloxClient.is_purchasable({"unitsAvailableForConsumption": 0, "totalQuantity": 100}),
        "sold-out limited is rejected",
    )

    bot = new_bot()
    cfg = {"asset_types": [11], "exclude_keywords": ["shirt"]}
    check(not bot.should_take(dict(FREE_META, assetType=8), cfg), "asset_types filter applies")
    check(bot.should_take(dict(FREE_META, assetType=11), cfg), "allowed asset type passes")
    check(
        not bot.should_take(dict(FREE_META, assetType=11, name="Free Shirt"), cfg),
        "exclude keyword applies",
    )


# --------------------------------------------------------------------------- #
# 2. fast path makes exactly one request
# --------------------------------------------------------------------------- #
async def test_fast_path_is_one_request() -> None:
    bot = new_bot()
    calls: list[str] = []

    async def fake(method, url, **kwargs):
        calls.append(url)
        if "marketplace-sales" in url:
            return {"purchased": True}
        raise AssertionError(f"fast path made an unnecessary read: {url}")

    async def no_ownership_check(*args, **kwargs):
        raise AssertionError("fast path must not call is_owned")

    bot.roblox._request = fake
    bot.roblox.is_owned = no_ownership_check
    bot.store.update(
        1, enabled=True, fast=True, category=None,
        watch_ids=[111], watch_meta={"111": dict(FREE_META)},
    )

    summary = await bot.cycle(1)
    check(summary["bought"] == 1, "fast path buys the item")
    check(len(calls) == 1, "fast path spends exactly one request")
    check("purchase-item" in calls[0], "that request is the purchase POST")
    await bot.close()


# --------------------------------------------------------------------------- #
# 3. stale cached metadata recovers
# --------------------------------------------------------------------------- #
async def test_stale_metadata_recovery() -> None:
    bot = new_bot()
    posts = 0

    async def fake(method, url, **kwargs):
        nonlocal posts
        if "marketplace-sales" in url:
            posts += 1
            if posts == 1:
                raise RobloxError("Expected price differs from actual price", 400)
            return {"purchased": True}
        if "catalog/items/details" in url:
            return {"data": [dict(FREE_META)]}
        raise AssertionError(url)

    bot.roblox._request = fake
    bot.store.update(
        1, enabled=True, fast=True, category=None,
        watch_ids=[111], watch_meta={"111": dict(PAID_META)},
    )

    summary = await bot.cycle(1)
    check(posts == 2, "stale metadata triggers one refresh and one retry")
    check(summary["bought"] == 1, "retry after refresh succeeds")
    await bot.close()


# --------------------------------------------------------------------------- #
# 4. "not yet" failures are cheap and are not persisted
# --------------------------------------------------------------------------- #
async def test_transient_failures() -> None:
    bot = new_bot()

    async def fake(method, url, **kwargs):
        if "marketplace-sales" in url:
            raise RobloxError("Item is not for sale", 400)
        if "catalog/items/details" in url:
            return {"data": [dict(PAID_META)]}
        raise AssertionError(url)

    bot.roblox._request = fake
    bot.store.update(
        1, enabled=True, fast=True, category=None,
        watch_ids=[111], watch_meta={"111": dict(PAID_META)},
    )

    await bot.cycle(1)
    cfg = bot.store.get(1)
    check(len(cfg["history"]) == 0, "transient failure is not written to history")
    check(bot.on_cooldown(1, 111), "transient failure backs off briefly")
    check(not bot.store.is_seen(1, 111), "transient failure does not blacklist the item")
    await bot.close()


# --------------------------------------------------------------------------- #
# 5. normal mode checks ownership and handles candidates in parallel
# --------------------------------------------------------------------------- #
async def test_normal_mode_parallel() -> None:
    bot = new_bot()
    items = [
        dict(FREE_META, id=900 + i, collectibleItemId=f"c{i}", collectibleProductId=f"p{i}")
        for i in range(3)
    ]

    async def fake(method, url, **kwargs):
        if "search/items/details" in url:
            return {"data": items, "nextPageCursor": None}
        if "is-owned" in url:
            return "false"
        if "marketplace-sales" in url:
            return {"purchased": True}
        raise AssertionError(url)

    bot.roblox._request = fake
    bot.store.update(1, enabled=True, fast=False, category=1, pages=1)

    started = time.perf_counter()
    summary = await bot.cycle(1)
    elapsed = (time.perf_counter() - started) * 1000

    check(summary["bought"] == 3, "all three free items bought")
    # Sequentially this would be ~1.4s at 150ms/request; parallel stays well under.
    check(elapsed < 900, f"candidates handled in parallel ({elapsed:.0f} ms)")
    await bot.close()


# --------------------------------------------------------------------------- #
# 6. the hot lane must not queue behind the bulk lane
# --------------------------------------------------------------------------- #
async def test_lane_independence() -> None:
    client = RobloxClient("selftest", min_request_gap=1.5, hot_gap=0.0)
    await client._throttle(False)  # bulk clock starts now
    started = time.perf_counter()
    await client._throttle(True)
    waited = (time.perf_counter() - started) * 1000
    check(waited < 150, f"hot lane ignores bulk gap ({waited:.0f} ms)")


# --------------------------------------------------------------------------- #
# 7. doctor report survives a dead cookie
# --------------------------------------------------------------------------- #
async def test_doctor_dead_cookie() -> None:
    client = RobloxClient("_|WARNING:-DO-NOT-SHARE-DEAD")

    async def fake(method, url, **kwargs):
        raise RobloxError("The login credentials are invalid.", 401)

    async def no_csrf():
        raise RobloxError("Could not obtain a CSRF token", 403)

    client._request = fake
    client.refresh_csrf = no_csrf

    report = await client.diagnose()
    check(not report["auth_ok"], "doctor reports a dead cookie")
    check(not report["csrf_ok"], "doctor reports the CSRF failure")
    check(client._csrf == "", "failed CSRF probe leaves no stale token behind")


# --------------------------------------------------------------------------- #
# 8. request budget: fast mode vs normal mode
# --------------------------------------------------------------------------- #
async def measure(fast: bool) -> tuple[int, float]:
    bot = new_bot()
    calls: list[str] = []

    async def fake(method, url, **kwargs):
        calls.append(url)
        await asyncio.sleep(RTT)
        if "is-owned" in url:
            return "false"
        if "items/details" in url:
            return {"data": [dict(FREE_META)], "nextPageCursor": None}
        if "purchase" in url:
            return {"purchased": True}
        raise AssertionError(url)

    bot.roblox._request = fake
    cfg = dict(enabled=True, category=None, watch_ids=[111])
    if fast:
        cfg.update(fast=True, watch_meta={"111": dict(FREE_META)})
    bot.store.update(1, **cfg)

    started = time.perf_counter()
    summary = await bot.cycle(1)
    elapsed = (time.perf_counter() - started) * 1000
    check(summary["bought"] == 1, f"{'fast' if fast else 'normal'} mode buys the item")
    await bot.close()
    return len(calls), elapsed


async def test_budget() -> None:
    fast_calls, fast_ms = await measure(True)
    normal_calls, normal_ms = await measure(False)
    print(
        f"   normal: {normal_calls} requests, {normal_ms:.0f} ms   "
        f"fast: {fast_calls} requests, {fast_ms:.0f} ms"
    )
    check(fast_calls < normal_calls, "fast mode uses fewer requests")
    check(fast_ms < normal_ms, "fast mode finishes sooner")


# --------------------------------------------------------------------------- #
# 8b. bulk grab only ever buys strictly-free items
# --------------------------------------------------------------------------- #
async def test_grab_only_buys_free() -> None:
    bot = new_bot()
    paid = dict(FREE_META, id=301, name="Paid Item", price=250, priceStatus="PriceLabel")
    free = dict(FREE_META, id=302, name="Free Item", price=0, priceStatus="Free")
    no_price = dict(FREE_META, id=303, name="Unknown Price", price=None, priceStatus="Free")
    repriced = dict(FREE_META, id=304, name="Was Free", price=0, priceStatus="Free")

    # Search claims all four are free; the fresh details call reprices two of them.
    claimed = [dict(paid, price=0, priceStatus="Free"), free, no_price, repriced]
    fresh_map = {
        301: paid,          # actually 250 Robux now
        302: free,          # genuinely free
        303: dict(no_price, priceStatus="PriceLabel"),  # price unknown -> never bought
        304: dict(repriced, price=99),  # repriced between search and buy
    }
    purchases: list[int] = []

    async def fake(method, url, **kwargs):
        if "search/items/details" in url:
            return {"data": claimed, "nextPageCursor": None}
        if "catalog/items/details" in url:
            return {"data": list(fresh_map.values())}
        if "is-owned" in url:
            return "false"
        if "marketplace-sales" in url or "purchases/products" in url:
            body = kwargs.get("json") or {}
            expected = body.get("expectedPrice")
            check(expected == 0, f"purchase sent expectedPrice={expected}, must be 0")
            purchases.append(1)
            return {"purchased": True}
        raise AssertionError(url)

    bot.roblox._request = fake
    bot.store.update(1, enabled=True, fast=False, category=1, pages=1)

    summary = await bot.grab_free(1, pages=1, limit=10)
    check(summary["matched"] == 4, "grab found all four candidates")
    check(summary["verified"] == 1, "only genuinely free items survive price verification")
    check(summary["bought"] == 1, "exactly one item bought")
    check(len(purchases) == 1, "exactly one purchase request sent")

    check(SniperBot.strictly_free({"price": 0}), "strictly_free accepts R$0")
    check(not SniperBot.strictly_free({"price": 5}), "strictly_free rejects R$5")
    check(SniperBot.strictly_free({"priceStatus": "Free"}), "strictly_free accepts priceStatus Free")
    check(not SniperBot.strictly_free({"price": None}), "strictly_free rejects unknown price")
    await bot.close()


# --------------------------------------------------------------------------- #
# 8c. catalog sweep: walks pages, stops on caps, resumes from the cursor
# --------------------------------------------------------------------------- #
def _sweep_item(i, price):
    return {
        "id": i, "name": f"Item {i}", "price": price, "priceStatus": "Free",
        "purchaseable": True, "assetType": 8, "creatorTargetId": 1,
        "collectibleItemId": f"c{i}", "collectibleProductId": f"p{i}", "itemType": "Asset",
    }


def _sweep_bot(pages, fresh_prices, delay=0.0):
    bot = new_bot()
    cursor_state = {"page": 0}

    async def fake(method, url, **kwargs):
        if "search/items/details" in url:
            if delay:
                await asyncio.sleep(delay)
            idx = cursor_state["page"]
            cursor_state["page"] += 1
            if idx >= len(pages):
                return {"data": [], "nextPageCursor": None}
            nxt = f"c{idx + 1}" if idx + 1 < len(pages) else None
            return {"data": pages[idx], "nextPageCursor": nxt}
        if "catalog/items/details" in url:
            ids = [int(x) for x in kwargs["params"]["items"].split(",")]
            return {"data": [_sweep_item(i, fresh_prices.get(i, 0)) for i in ids]}
        if "is-owned" in url:
            return "false"
        if "marketplace-sales" in url or "purchases/products" in url:
            return {"purchased": True}
        raise AssertionError(url)

    bot.roblox._request = fake
    bot.store.update(1, enabled=False, channel_id=None, category=1)
    return bot


async def _await_sweep(bot):
    task = bot.sweeps.get(1)
    if task:
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_sweep() -> None:
    pages = [[_sweep_item(1, 0), _sweep_item(2, 0)], [_sweep_item(3, 0)]]

    # Walks every page; item 2 turns out to cost R$50 when re-checked.
    bot = _sweep_bot(pages, {1: 0, 2: 50, 3: 0})
    bot.start_sweep(1, category=1, max_pages=10)
    await _await_sweep(bot)
    st = bot.sweep_state(1)
    check(st["pages"] == 2, "sweep walked both pages")
    check(st["scanned"] == 3, "sweep counted every scanned item")
    check(st["verified"] == 2, "sweep verified only the genuinely free items")
    check(st["bought"] == 2, "sweep bought only free items")
    check(st["reason"] == "end of catalog" and not st["active"], "sweep ends cleanly")
    await bot.close()

    # Purchase cap is enforced mid-page, not just between pages.
    bot = _sweep_bot(pages, {1: 0, 2: 0, 3: 0})
    bot.start_sweep(1, category=1, max_pages=10, max_purchases=1)
    await _await_sweep(bot)
    st = bot.sweep_state(1)
    check(st["bought"] == 1 and st["reason"] == "purchase cap reached", "purchase cap is exact")
    await bot.close()

    # Cursor survives a page cap, so a later run can resume.
    bot = _sweep_bot(pages, {1: 0, 2: 0, 3: 0})
    bot.start_sweep(1, category=1, max_pages=1)
    await _await_sweep(bot)
    st = bot.sweep_state(1)
    check(st["pages"] == 1 and st["cursor"] == "c1", "page cap keeps the cursor for resume")
    await bot.close()

    # Stopping mid-run leaves a resumable state.
    bot = _sweep_bot(pages * 60, {i: 0 for i in range(300)}, delay=0.02)
    bot.start_sweep(1, category=1, max_pages=500)
    await asyncio.sleep(0.05)
    await bot.stop_sweep(1)
    st = bot.sweep_state(1)
    check(not st["active"] and st["reason"] == "stopped", "sweep stops on request")
    check(st["cursor"] is not None, "stopped sweep keeps a resume point")
    await bot.close()


# --------------------------------------------------------------------------- #
# 9. state store round trip
# --------------------------------------------------------------------------- #
def test_store() -> None:
    bot = new_bot()
    bot.store.update(1, enabled=True, channel_id=42)
    bot.store.mark_seen(1, 555)
    bot.store.add_history(1, {"item_id": 555, "name": "hat", "ok": True, "message": "bought"})

    reloaded = Store(bot.store.path)
    cfg = reloaded.get(1)
    check(cfg["enabled"] and cfg["channel_id"] == 42, "config persists")
    check(reloaded.is_seen(1, 555), "seen ids persist")
    check(len(cfg["history"]) == 1, "history persists")
    check(cfg["fast"] is False and cfg["watch_meta"] == {}, "new fields have defaults")


async def main() -> int:
    tests = [
        ("filters", test_filters),
        ("store", test_store),
        ("fast path", test_fast_path_is_one_request),
        ("stale metadata", test_stale_metadata_recovery),
        ("transient failures", test_transient_failures),
        ("parallel candidates", test_normal_mode_parallel),
        ("lane independence", test_lane_independence),
        ("doctor", test_doctor_dead_cookie),
        ("grab only free", test_grab_only_buys_free),
        ("catalog sweep", test_sweep),
        ("request budget", test_budget),
    ]

    failures = 0
    print("Roblox sniper self-test (offline)\n")
    for name, fn in tests:
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                await result
            print(f"  ✓ {name}")
        except Exception as exc:
            failures += 1
            print(f"  ✗ {name}: {exc}")
            traceback.print_exc()

    print(f"\n{len(PASSED)} checks passed, {failures} test(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
