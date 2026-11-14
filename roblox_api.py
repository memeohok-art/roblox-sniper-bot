"""Async Roblox web-API client for a free-item sniper.

Only the handful of endpoints a sniper actually needs:

  * authenticate a ``.ROBLOSECURITY`` cookie and resolve the user id
  * CSRF handshake required by every POST
  * catalog search / item details  (catalog.roblox.com)
  * ownership check                (inventory.roblox.com)
  * purchase:
      - economy product purchase   (ordinary free assets, T-shirts, gamepasses...)
      - marketplace-sales          (limited UGC / collectibles, incl. free limiteds)

Everything is rate limited by a single global gap and retried on 429/5xx so one
slow endpoint cannot spin the client into a ban.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import aiohttp

log = logging.getLogger(__name__)

CATALOG = "https://catalog.roblox.com/v1"
ECONOMY = "https://economy.roblox.com"
INVENTORY = "https://inventory.roblox.com"
USERS = "https://users.roblox.com"
AUTH = "https://auth.roblox.com"
MARKETPLACE = "https://apis.roblox.com/marketplace-sales/v1"
THUMBS = "https://thumbnails.roblox.com/v1"

# A current desktop UA. Roblox serves different (and sometimes broken) responses
# to obviously-scripted clients.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Errors that mean "you can never buy this", so the sniper should stop retrying.
TERMINAL_MESSAGES = (
    "already own",
    "insufficient funds",
    "not enough robux",
    "you do not have permission",
    "cannot be purchased",
    "item is not for sale",
    "sold out",
    "no longer available",
    "too many attempts",
    "flood",
)


class RobloxError(Exception):
    def __init__(self, message: str, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload

    @property
    def terminal(self) -> bool:
        text = str(self).lower()
        return any(m in text for m in TERMINAL_MESSAGES)

    @property
    def retryable(self) -> bool:
        return self.status in (429, 500, 502, 503, 504)


@dataclass
class PurchaseResult:
    ok: bool
    message: str
    terminal: bool = False


def _error_text(payload: Any, status: int | None) -> str:
    """Pull the most human-readable message out of a Roblox error body."""
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            first = errors[0]
            if isinstance(first, dict):
                return str(first.get("message") or first.get("code") or first)
            return str(first)
        for key in ("errorMsg", "message", "shortMessage", "error"):
            value = payload.get(key)
            if value:
                return str(value)
    if isinstance(payload, str) and payload.strip():
        return payload.strip()[:300]
    return f"HTTP {status}"


class RobloxClient:
    """One authenticated Roblox account. Create one per cookie.

    Requests travel on two lanes:

    * **bulk** (`search_items`, background scans) — paced by `min_request_gap`
    * **hot** (`purchase`, watch-list details) — paced by the much smaller `hot_gap`

    The lanes have independent clocks and locks, so a slow catalog page can never
    delay a purchase that is already in flight.
    """

    def __init__(
        self,
        cookie: str,
        *,
        min_request_gap: float = 1.2,
        hot_gap: float = 0.35,
        request_timeout: float = 25.0,
        max_retries: int = 3,
        max_connections: int = 32,
    ) -> None:
        if not cookie or not cookie.strip():
            raise ValueError("A .ROBLOSECURITY cookie is required")
        self.cookie = cookie.strip()
        self.min_request_gap = max(0.4, float(min_request_gap))
        self.hot_gap = max(0.0, float(hot_gap))
        self.request_timeout = request_timeout
        self.max_retries = max(1, int(max_retries))
        self.max_connections = max(4, int(max_connections))

        self.user_id: int = 0
        self.username: str = ""
        self.last_latency_ms: int = 0

        self._session: aiohttp.ClientSession | None = None
        self._csrf: str = ""
        self._bulk_lock = asyncio.Lock()
        self._hot_lock = asyncio.Lock()
        self._last_bulk_at: float = 0.0
        self._last_hot_at: float = 0.0

    # ------------------------------------------------------------------ setup
    async def start(self) -> "RobloxClient":
        if self._session is None or self._session.closed:
            # A pooled connector keeps TLS sessions warm — the handshake costs more
            # than the request itself when you poll every second.
            connector = aiohttp.TCPConnector(
                limit=self.max_connections,
                limit_per_host=16,
                ttl_dns_cache=300,
                keepalive_timeout=45,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=self.request_timeout),
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
            )
        return self

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "RobloxClient":
        return await self.start()

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("Call `await client.start()` first")
        return self._session

    @property
    def cookie_header(self) -> str:
        if self.cookie.lower().startswith(".roblosecurity="):
            return self.cookie
        return f".ROBLOSECURITY={self.cookie}"

    # --------------------------------------------------------------- plumbing
    async def _throttle(self, hot: bool = False) -> None:
        """Keep the configured gap within one lane; lanes never block each other."""
        lock = self._hot_lock if hot else self._bulk_lock
        gap = self.hot_gap if hot else self.min_request_gap
        loop = asyncio.get_running_loop()
        async with lock:
            last = self._last_hot_at if hot else self._last_bulk_at
            wait = last + gap - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            if hot:
                self._last_hot_at = loop.time()
            else:
                self._last_bulk_at = loop.time()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth: bool = True,
        needs_csrf: bool = False,
        hot: bool = False,
        timeout: float | None = None,
    ) -> Any:
        last_error: RobloxError | None = None

        for attempt in range(self.max_retries):
            await self._throttle(hot)
            headers: dict[str, str] = {}
            if auth:
                headers["Cookie"] = self.cookie_header
            if needs_csrf:
                if not self._csrf:
                    await self.refresh_csrf()
                headers["X-CSRF-TOKEN"] = self._csrf

            started = time.monotonic()
            try:
                async with self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout) if timeout else None,
                ) as resp:
                    text = await resp.text()
                    status = resp.status
                    self.last_latency_ms = int((time.monotonic() - started) * 1000)

                    # Roblox answers 403 + x-csrf-token when the token is missing/stale.
                    if status == 403 and needs_csrf:
                        token = resp.headers.get("x-csrf-token")
                        if token:
                            self._csrf = token
                            continue

                    if status == 429:
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after else 2.0 * (attempt + 1)
                        delay += random.uniform(0, 1.5)
                        log.warning("429 from %s, backing off %.1fs", url, delay)
                        await asyncio.sleep(delay)
                        last_error = RobloxError("Rate limited by Roblox (429)", 429)
                        continue

                    if status >= 500:
                        await asyncio.sleep(1.5 * (attempt + 1) + random.random())
                        last_error = RobloxError(_error_text(text, status), status)
                        continue

                    payload: Any
                    if text:
                        try:
                            payload = await resp.json(content_type=None)
                        except Exception:
                            payload = text
                    else:
                        payload = None

                    if status >= 400:
                        raise RobloxError(_error_text(payload, status), status, payload)

                    return payload

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Timeouts are not ClientError subclasses on every Python version,
                # so both are handled here.
                last_error = RobloxError(f"Network error: {exc!r}")
                await asyncio.sleep(1.0 + attempt)

        raise last_error or RobloxError("Request failed")

    # ----------------------------------------------------------------- auth
    async def refresh_csrf(self) -> str:
        """Fetch a CSRF token. Roblox hands it out on a rejected POST."""
        await self._throttle(hot=True)
        async with self.session.post(
            f"{AUTH}/v2/logout", headers={"Cookie": self.cookie_header}
        ) as resp:
            token = resp.headers.get("x-csrf-token")
            if not token:
                raise RobloxError(
                    "Could not obtain a CSRF token — the cookie is probably invalid or expired",
                    resp.status,
                )
            self._csrf = token
            return token

    async def authenticate(self) -> dict[str, Any]:
        data = await self._request("GET", f"{USERS}/v1/users/authenticated")
        if not isinstance(data, dict) or not data.get("id"):
            raise RobloxError("Cookie is not tied to a valid account")
        self.user_id = int(data["id"])
        self.username = str(data.get("name") or "")
        # Warm the CSRF token so the first purchase isn't delayed.
        try:
            await self.refresh_csrf()
        except RobloxError:
            log.warning("Authenticated but could not prefetch a CSRF token")
        log.info("Authenticated as %s (%s)", self.username, self.user_id)
        return data

    async def diagnose(self) -> dict[str, Any]:
        """Read-only health checks behind `/snipe doctor`.

        Every step is isolated so one failure still yields a full report, and
        nothing here buys, spends, or mutates the account.
        """
        report: dict[str, Any] = {
            "cookie_present": bool(self.cookie),
            "cookie_length": len(self.cookie),
            "cookie_prefixed": self.cookie.lower().startswith("_|warning"),
            "username": "",
            "user_id": 0,
            "auth_ok": False,
            "auth_error": None,
            "auth_ms": None,
            "csrf_ok": False,
            "csrf_error": None,
            "csrf_ms": None,
            "robux": None,
            "robux_error": None,
            "catalog_ok": False,
            "catalog_error": None,
            "catalog_items": 0,
        }

        started = time.monotonic()
        try:
            data = await self._request("GET", f"{USERS}/v1/users/authenticated")
            if isinstance(data, dict) and data.get("id"):
                report["auth_ok"] = True
                report["username"] = str(data.get("name") or "")
                report["user_id"] = int(data["id"])
                self.user_id = report["user_id"]
                self.username = report["username"]
            else:
                report["auth_error"] = "Response contained no account id"
        except RobloxError as exc:
            report["auth_error"] = str(exc)
        report["auth_ms"] = int((time.monotonic() - started) * 1000)

        # Force a fresh handshake — a cached token would prove nothing.
        self._csrf = ""
        started = time.monotonic()
        try:
            await self.refresh_csrf()
            report["csrf_ok"] = True
        except RobloxError as exc:
            report["csrf_error"] = str(exc)
        report["csrf_ms"] = int((time.monotonic() - started) * 1000)

        try:
            data = await self._request("GET", f"{ECONOMY}/v1/user/currency")
            if isinstance(data, dict) and data.get("robux") is not None:
                report["robux"] = int(data["robux"])
            else:
                report["robux_error"] = "No robux field in response"
        except RobloxError as exc:
            report["robux_error"] = str(exc)

        try:
            items, _cursor = await self.search_items(category=1, sort_type=3, limit=10)
            report["catalog_items"] = len(items)
            report["catalog_ok"] = bool(items)
            if not items:
                report["catalog_error"] = "Catalog returned no items"
        except RobloxError as exc:
            report["catalog_error"] = str(exc)

        return report

    async def robux_balance(self) -> int | None:
        try:
            data = await self._request("GET", f"{ECONOMY}/v1/user/currency")
        except RobloxError as exc:
            log.debug("balance lookup failed: %s", exc)
            return None
        if isinstance(data, dict) and data.get("robux") is not None:
            try:
                return int(data["robux"])
            except (TypeError, ValueError):
                return None
        return None

    # --------------------------------------------------------------- catalog
    async def search_items(
        self,
        *,
        category: int = 1,
        subcategory: int | None = None,
        keyword: str | None = None,
        sort_type: int = 3,
        sales_type_filter: int = 1,
        limit: int = 30,
        cursor: str | None = None,
        creator_target_id: int | None = None,
        creator_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of catalog search. Returns (items, next_cursor)."""
        params: dict[str, Any] = {
            "Category": category,
            "Limit": max(10, min(30, int(limit))),
            "SortType": sort_type,
            "SalesTypeFilter": sales_type_filter,
        }
        if subcategory:
            params["Subcategory"] = subcategory
        if keyword:
            params["Keyword"] = keyword
        if cursor:
            params["Cursor"] = cursor
        if creator_target_id:
            params["CreatorTargetId"] = creator_target_id
        if creator_type:
            params["CreatorType"] = creator_type

        data = await self._request("GET", f"{CATALOG}/search/items/details", params=params)
        if not isinstance(data, dict):
            return [], None
        if "data" not in data and data.get("errors"):
            # Roblox answers 200 with an empty `errors` block for unauthenticated
            # or badly-shaped catalog queries — surface it instead of scanning nothing.
            log.warning("Catalog search returned no data: %s", data.get("errors"))
            return [], None
        items = [i for i in (data.get("data") or []) if isinstance(i, dict)]
        return items, data.get("nextPageCursor")

    async def item_details(self, item_ids: Sequence[int]) -> list[dict[str, Any]]:
        """Full details for up to 100 ids (assets and bundles alike)."""
        ids = [str(int(i)) for i in item_ids][:100]
        if not ids:
            return []
        data = await self._request(
            "GET", f"{CATALOG}/catalog/items/details", params={"items": ",".join(ids)}
        )
        if not isinstance(data, dict):
            return []
        return [i for i in (data.get("data") or []) if isinstance(i, dict)]

    async def thumbnails(self, asset_ids: Sequence[int]) -> dict[int, str]:
        """Map assetId -> image URL (420x420 png)."""
        ids = [str(int(i)) for i in asset_ids][:100]
        if not ids:
            return {}
        try:
            data = await self._request(
                "GET",
                f"{THUMBS}/assets",
                params={
                    "assetIds": ",".join(ids),
                    "size": "420x420",
                    "format": "Png",
                    "isCircular": "false",
                },
            )
        except RobloxError:
            return {}
        out: dict[int, str] = {}
        for entry in (data or {}).get("data", []) if isinstance(data, dict) else []:
            if isinstance(entry, dict) and entry.get("imageUrl"):
                out[int(entry.get("targetId", 0))] = entry["imageUrl"]
        return out

    # ------------------------------------------------------------- ownership
    async def is_owned(self, asset_id: int, asset_type: int | str) -> bool | None:
        """True/False, or None when the check itself failed."""
        if not self.user_id:
            return None
        try:
            data = await self._request(
                "GET",
                f"{INVENTORY}/v1/users/{self.user_id}/items/{asset_type}/{int(asset_id)}/is-owned",
            )
        except RobloxError:
            return None
        if isinstance(data, bool):
            return data
        if isinstance(data, str):
            return data.strip().lower() == "true"
        if isinstance(data, dict):
            return bool(data.get("isOwned", data.get("owned")))
        return None

    # -------------------------------------------------------------- purchase
    @staticmethod
    def is_free(item: dict[str, Any]) -> bool:
        price = item.get("price")
        lowest = item.get("lowestPrice")
        status = str(item.get("priceStatus") or "").lower()
        return price == 0 or lowest == 0 or status == "free"

    @staticmethod
    def is_purchasable(item: dict[str, Any]) -> bool:
        if item.get("purchaseable") is False:
            return False
        if item.get("isPurchasable") is False:
            return False
        if item.get("itemStatus") not in (None, [], ""):
            return False
        units = item.get("unitsAvailableForConsumption")
        if isinstance(units, int) and units == 0 and item.get("totalQuantity"):
            return False
        return True

    async def purchase(self, item: dict[str, Any], *, hot: bool = True) -> PurchaseResult:
        """Buy `item` at its current price. Never raises for expected failures.

        Hot by default: a purchase is the one call that must never queue behind a
        background catalog scan.
        """
        hot_timeout = 10.0 if hot else None
        item_id = item.get("id") or item.get("itemId")
        name = item.get("name") or f"item {item_id}"
        price = int(item.get("price") or item.get("lowestPrice") or 0)
        seller_id = item.get("creatorTargetId")
        collectible_item_id = item.get("collectibleItemId")
        collectible_product_id = item.get("collectibleProductId")
        product_id = item.get("productId")

        try:
            if collectible_item_id and collectible_product_id:
                payload = {
                    "collectibleProductId": collectible_product_id,
                    "expectedCurrency": 1,
                    "expectedPrice": price,
                    "expectedSellerId": seller_id,
                    "expectedPurchaserId": self.user_id,
                    "idempotencyKey": str(uuid.uuid4()),
                }
                data = await self._request(
                    "POST",
                    f"{MARKETPLACE}/item/{collectible_item_id}/purchase-item",
                    json=payload,
                    needs_csrf=True,
                    hot=hot,
                    timeout=hot_timeout,
                )
                if isinstance(data, dict) and data.get("purchased") is False:
                    reason = data.get("reason") or data.get("status") or "rejected"
                    return PurchaseResult(False, f"Rejected: {reason}", terminal=True)
                return PurchaseResult(True, f"Bought {name} for R${price}")

            if product_id:
                payload = {
                    "expectedCurrency": 1,
                    "expectedPrice": price,
                    "expectedSellerId": seller_id,
                }
                data = await self._request(
                    "POST",
                    f"{ECONOMY}/v1/purchases/products/{product_id}",
                    json=payload,
                    needs_csrf=True,
                    hot=hot,
                    timeout=hot_timeout,
                )
                if isinstance(data, dict):
                    if data.get("purchased") is True:
                        return PurchaseResult(True, f"Bought {name} for R${price}")
                    reason = data.get("reason") or data.get("errorMsg") or "rejected"
                    return PurchaseResult(False, f"Rejected: {reason}", terminal=True)
                return PurchaseResult(False, "Unexpected purchase response")

            return PurchaseResult(False, "Item has no purchasable product id", terminal=True)

        except RobloxError as exc:
            # A stale CSRF token is the one failure worth retrying once.
            if exc.status == 403 and "csrf" in str(exc).lower():
                self._csrf = ""
                try:
                    return await self.purchase(item, hot=hot)
                except RobloxError as retry_exc:
                    return PurchaseResult(False, str(retry_exc), retry_exc.terminal)
            return PurchaseResult(False, str(exc), exc.terminal)

    # ---------------------------------------------------------------- warmup
    async def warmup(self, watch_ids: Iterable[int] = ()) -> None:
        """Pre-open a pooled connection, cache a CSRF token and fetch item metadata.

        Doing this once up front removes the TLS handshake + CSRF round trip from
        the first hot poll, which is exactly the poll that matters.
        """
        try:
            await self.refresh_csrf()
        except RobloxError as exc:
            log.debug("Warmup: CSRF unavailable (%s)", exc)
        try:
            await self._request(
                "GET",
                f"{CATALOG}/search/items/details",
                params={"Category": 1, "Limit": 10, "SortType": 3, "SalesTypeFilter": 1},
                hot=True,
            )
        except RobloxError as exc:
            log.debug("Warmup: catalog unreachable (%s)", exc)
        ids = [int(i) for i in watch_ids][:100]
        if ids:
            try:
                await self._request(
                    "GET", f"{CATALOG}/catalog/items/details", params={"items": ",".join(map(str, ids))}
                )
            except RobloxError as exc:
                log.debug("Warmup: item details failed (%s)", exc)
