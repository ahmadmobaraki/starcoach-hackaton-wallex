from __future__ import annotations

import asyncio
import logging
import random
import time
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from wallex_trader.config import Settings
from wallex_trader.exchange.auth import private_headers, public_headers
from wallex_trader.exchange.rate_limiter import SlidingWindowLimiter, TokenBucket
from wallex_trader.models import (
    BookLevel,
    Candle,
    FeeRate,
    MarketMeta,
    OrderBook,
    Side,
    TradeTick,
)

logger = logging.getLogger("trader.rest")


class WallexAPIError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: Any = None,
        payload: Any = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload
        self.retryable = retryable


class WallexAuthError(WallexAPIError):
    pass


class WallexRateLimitError(WallexAPIError):
    pass


def _result(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


class WallexRESTClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.public_bucket = TokenBucket(settings.wallex_public_rate_limit, capacity=5)
        self.order_bucket = SlidingWindowLimiter(
            limit=settings.order_rate_limit_per_10s, window_sec=10.0
        )
        self._client: httpx.AsyncClient | None = None
        self.latencies: deque[float] = deque(maxlen=500)
        self.calls = 0
        self.errors = 0
        self.last_server_time: datetime | None = None
        self.clock_skew_sec: float | None = None

    async def __aenter__(self) -> WallexRESTClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = httpx.AsyncClient(
            base_url=self.settings.wallex_api_base.rstrip("/"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            http2=True,
            headers=public_headers(),
            follow_redirects=True,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("REST client is not connected")
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        private: bool = False,
        retries: int = 3,
        order_request: bool = False,
    ) -> Any:
        if order_request:
            await self.order_bucket.acquire()
        await self.public_bucket.acquire()
        headers = private_headers(self.settings) if private else {}
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            self.calls += 1
            try:
                response = await self.client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=headers or None,
                )
                self.latencies.append((time.perf_counter() - started) * 1000)
                date_header = response.headers.get("date")
                if date_header:
                    try:
                        self.last_server_time = parsedate_to_datetime(date_header).astimezone(UTC)
                        self.clock_skew_sec = (
                            datetime.now(UTC) - self.last_server_time
                        ).total_seconds()
                    except (TypeError, ValueError):
                        pass
                if response.status_code in (401, 403):
                    self.errors += 1
                    raise WallexAuthError(
                        f"Wallex authorization failed ({response.status_code})",
                        status=response.status_code,
                        payload=response.text[:500],
                    )
                if response.status_code == 429:
                    self.errors += 1
                    retry_after = _retry_after_seconds(response)
                    if attempt >= retries:
                        raise WallexRateLimitError(
                            "Wallex rate limit exceeded",
                            status=429,
                            payload=response.text[:500],
                            retryable=True,
                        )
                    await asyncio.sleep(retry_after or (0.5 * 2**attempt + random.random() * 0.3))
                    continue
                if response.status_code >= 500:
                    self.errors += 1
                    error = WallexAPIError(
                        f"Wallex server error {response.status_code}",
                        status=response.status_code,
                        payload=response.text[:500],
                        retryable=True,
                    )
                    if attempt >= retries or order_request:
                        raise error
                    last_error = error
                    await asyncio.sleep(0.4 * 2**attempt + random.random() * 0.3)
                    continue
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise WallexAPIError(
                        "Wallex returned non-JSON response",
                        status=response.status_code,
                        payload=response.text[:500],
                    ) from exc
                if response.status_code >= 400:
                    message, code = _error_message(payload)
                    self.errors += 1
                    raise WallexAPIError(
                        message,
                        status=response.status_code,
                        code=code,
                        payload=payload,
                    )
                if isinstance(payload, dict) and payload.get("success") is False:
                    message, code = _error_message(payload)
                    self.errors += 1
                    raise WallexAPIError(message, status=response.status_code, code=code, payload=payload)
                return _result(payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.errors += 1
                last_error = exc
                if attempt >= retries or order_request:
                    raise WallexAPIError(
                        f"Wallex transport error: {exc}",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(0.4 * 2**attempt + random.random() * 0.3)
        raise WallexAPIError(f"Wallex request failed: {last_error}", retryable=True)

    async def markets_raw(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/hector/web/v1/markets")
        markets = result.get("markets", []) if isinstance(result, dict) else []
        return list(markets)

    async def market_metadata(self) -> dict[str, MarketMeta]:
        hector = await self.markets_raw()
        symbols_payload: dict[str, Any] = {}
        try:
            v1 = await self._request("GET", "/v1/markets")
            symbols_payload = v1.get("symbols", {}) if isinstance(v1, dict) else {}
        except WallexAPIError as exc:
            logger.warning("v1 markets unavailable: %s", exc)
        result: dict[str, MarketMeta] = {}
        for item in hector:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            flags = [str(flag) for flag in item.get("flags", []) or []]
            created = _parse_dt(item.get("created_at"))
            v1_item = symbols_payload.get(symbol, {}) if isinstance(symbols_payload, dict) else {}
            stats = v1_item.get("stats", {}) if isinstance(v1_item, dict) else {}
            result[symbol] = MarketMeta(
                symbol=symbol,
                base_asset=str(item.get("base_asset", "")),
                quote_asset=str(item.get("quote_asset", "")),
                price_precision=int(item.get("price_precision", 0) or 0),
                amount_precision=int(item.get("amount_precision", 0) or 0),
                price=_float(item.get("price"), _float(stats.get("lastPrice"))),
                quote_volume_24h=_float(item.get("quote_volume_24h")),
                volume_24h=_float(item.get("volume_24h")),
                change_24h=_float(item.get("change_24h")),
                min_qty=_float(v1_item.get("minQty")),
                min_notional=_float(v1_item.get("minNotional"), 50_000.0),
                tick_size=_float(v1_item.get("tickSize")),
                step_size=_float(v1_item.get("stepSize")),
                created_at=created,
                is_spot=bool(item.get("is_spot", True)),
                is_tmn_based=bool(item.get("is_tmn_based", False)),
                is_risky=any("risky" in flag.lower() for flag in flags),
                is_new=bool(item.get("is_new", False) or item.get("spot_is_new", False)),
                flags=flags,
            )
        return result

    async def depth(self, symbol: str, *, limit: int | None = None) -> OrderBook:
        result = await self._request("GET", "/v1/depth", params={"symbol": symbol})
        bids = _levels(result.get("bid", []) if isinstance(result, dict) else [])
        asks = _levels(result.get("ask", []) if isinstance(result, dict) else [])
        bids.sort(key=lambda level: level.price, reverse=True)
        asks.sort(key=lambda level: level.price)
        if limit:
            bids = bids[:limit]
            asks = asks[:limit]
        return OrderBook(symbol=symbol, bids=bids, asks=asks, updated_at=_now(), source="rest")

    async def trades(self, symbol: str) -> list[TradeTick]:
        result = await self._request("GET", "/v1/trades", params={"symbol": symbol})
        items = result.get("latestTrades", []) if isinstance(result, dict) else []
        trades: list[TradeTick] = []
        for item in items:
            try:
                trades.append(
                    TradeTick(
                        symbol=symbol,
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item["quantity"])),
                        is_buy_order=bool(item.get("isBuyOrder", False)),
                        timestamp=_parse_dt(item.get("timestamp")) or _now(),
                        trade_id=str(item.get("id")) if item.get("id") is not None else None,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        trades.sort(key=lambda trade: trade.timestamp)
        return trades

    async def candles(
        self,
        symbol: str,
        *,
        resolution: str = "1",
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        params = {
            "symbol": symbol,
            "resolution": resolution,
            "from": int(start.timestamp()),
            "to": int(end.timestamp()),
        }
        result = await self._request("GET", "/v1/udf/history", params=params)
        if not isinstance(result, dict) or result.get("s") != "ok":
            return []
        timestamps = result.get("t") or []
        opens = result.get("o") or []
        highs = result.get("h") or []
        lows = result.get("l") or []
        closes = result.get("c") or []
        volumes = result.get("v") or []
        candles: list[Candle] = []
        for idx, ts in enumerate(timestamps):
            try:
                candles.append(
                    Candle(
                        symbol=symbol,
                        ts=datetime.fromtimestamp(int(ts), tz=UTC),
                        open=float(opens[idx]),
                        high=float(highs[idx]),
                        low=float(lows[idx]),
                        close=float(closes[idx]),
                        volume=float(volumes[idx]),
                    )
                )
            except (IndexError, TypeError, ValueError):
                continue
        return candles

    async def balances(self) -> dict[str, dict[str, Any]]:
        return await self.balances_for_account(
            (self.settings.wallex_sub_account_client_id or "").strip() or None
        )

    async def balances_for_account(
        self, sub_account: str | None
    ) -> dict[str, dict[str, Any]]:
        """Read balances for the key account or an explicitly selected sub-account.

        This is also used at live startup to catch the dangerous case where the
        API key belongs to the main account but a sub-account header is present.
        """
        if not self.settings.has_wallex_key:
            raise ValueError("WALLEX_API_KEY is not configured")
        await self.public_bucket.acquire()
        headers = {
            "x-api-key": self.settings.wallex_api_key.get_secret_value(),
            "Accept": "application/json",
        }
        if sub_account:
            headers["sub-account-client-id"] = sub_account
        response = await self.client.get("/v1/account/balances", headers=headers)
        if response.status_code in (401, 403):
            raise WallexAuthError(
                f"Wallex authorization failed ({response.status_code})",
                status=response.status_code,
                payload=response.text[:300],
            )
        if response.status_code >= 400:
            raise WallexAPIError(
                f"Wallex balances failed ({response.status_code})",
                status=response.status_code,
                payload=response.text[:300],
            )
        payload = response.json()
        result = payload.get("result", payload) if isinstance(payload, dict) else {}
        return dict(result.get("balances", {})) if isinstance(result, dict) else {}

    async def fees(self) -> dict[str, FeeRate]:
        result = await self._request("GET", "/v1/account/fee", private=True)
        fees: dict[str, FeeRate] = {}
        if not isinstance(result, dict):
            return fees
        for symbol, item in result.items():
            if not isinstance(item, dict):
                continue
            try:
                fees[symbol] = FeeRate(
                    maker=float(item.get("makerFeeRate", 0)),
                    taker=float(item.get("takerFeeRate", 0)),
                )
            except (TypeError, ValueError):
                continue
        return fees

    async def open_orders(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/v1/account/openOrders", private=True)
        if isinstance(result, dict):
            return list(result.get("orders", []))
        if isinstance(result, list):
            return result
        return []

    async def order_history(self, limit: int = 100) -> list[dict[str, Any]]:
        result = await self._request(
            "GET", "/v1/account/orders", params={"per_page": limit}, private=True
        )
        if isinstance(result, dict):
            raw = result.get("orders") or result.get("data") or []
            return list(raw) if isinstance(raw, list) else []
        if isinstance(result, list):
            return result
        return []

    async def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        try:
            result = await self._request(
                "GET", f"/v1/account/orders/{client_order_id}", private=True, retries=1
            )
            if isinstance(result, dict):
                return result.get("order", result)
        except WallexAPIError as exc:
            if exc.status not in (404, 400):
                raise
        for order in await self.order_history(limit=200):
            if str(order.get("clientOrderId") or order.get("client_id")) == client_order_id:
                return order
        return None

    async def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = await self._request(
            "POST",
            "/v1/account/orders",
            json_body=payload,
            private=True,
            order_request=True,
            retries=0,
        )
        if isinstance(result, dict):
            return result
        return {"raw": result}

    async def cancel_order(self, client_order_id: str) -> dict[str, Any]:
        result = await self._request(
            "DELETE",
            f"/v1/account/orders/{client_order_id}",
            private=True,
            order_request=True,
            retries=0,
        )
        return result if isinstance(result, dict) else {"raw": result}

    async def cancel_all(self, symbol: str | None = None) -> int:
        canceled = 0
        for order in await self.open_orders():
            if symbol and str(order.get("symbol")) != symbol:
                continue
            client_id = str(order.get("clientOrderId") or order.get("client_id") or "")
            if not client_id:
                continue
            try:
                await self.cancel_order(client_id)
                canceled += 1
            except WallexAPIError as exc:
                logger.warning("cancel failed for %s: %s", client_id, exc)
        return canceled

    async def probe_server_time(self) -> float | None:
        await self._request("GET", "/v1/markets", retries=1)
        return self.clock_skew_sec

    def stats(self) -> dict[str, Any]:
        sorted_latencies = sorted(self.latencies)
        p50 = sorted_latencies[len(sorted_latencies) // 2] if sorted_latencies else 0.0
        p95 = (
            sorted_latencies[int(len(sorted_latencies) * 0.95) - 1]
            if sorted_latencies
            else 0.0
        )
        return {
            "calls": self.calls,
            "errors": self.errors,
            "error_rate": (self.errors / self.calls) if self.calls else 0.0,
            "latency_p50_ms": round(p50, 1),
            "latency_p95_ms": round(p95, 1),
            "clock_skew_sec": self.clock_skew_sec,
        }

    async def close_all_positions_info(self) -> dict[str, Decimal]:
        balances = await self.balances()
        return {
            asset: Decimal(str(item.get("value", 0)))
            for asset, item in balances.items()
            if Decimal(str(item.get("value", 0))) > 0
        }


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "" or value == "-":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _levels(raw: list[Any]) -> list[BookLevel]:
    levels: list[BookLevel] = []
    for item in raw:
        try:
            if isinstance(item, dict):
                levels.append(
                    BookLevel(
                        price=Decimal(str(item["price"])),
                        quantity=Decimal(str(item["quantity"])),
                    )
                )
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                levels.append(BookLevel(price=Decimal(str(item[0])), quantity=Decimal(str(item[1]))))
        except (KeyError, ValueError, TypeError):
            continue
    return levels


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(UTC)


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _error_message(payload: Any) -> tuple[str, Any]:
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or "Wallex API error")
        code = payload.get("code")
        return message, code
    return "Wallex API error", None


def parse_order_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the varying Wallex order response shapes."""
    order = payload.get("order") if isinstance(payload.get("order"), dict) else payload
    if not isinstance(order, dict):
        order = payload
    fills = order.get("fills") or order.get("trades") or []
    return {
        "exchange_order_id": order.get("id") or order.get("orderId") or order.get("order_id"),
        "client_order_id": order.get("clientOrderId") or order.get("client_id"),
        "status": str(order.get("status") or order.get("state") or "UNKNOWN").upper(),
        "filled_quantity": order.get("executedQuantity")
        or order.get("filledQuantity")
        or order.get("filled_quantity")
        or 0,
        "avg_price": order.get("averagePrice")
        or order.get("avgPrice")
        or order.get("avg_price")
        or 0,
        "fee": order.get("fee") or 0,
        "fee_asset": order.get("feeAsset") or order.get("fee_asset") or "TMN",
        "fills": fills,
        "raw": payload,
    }


def opposite_side(side: Side) -> Side:
    return Side.SELL if side == Side.BUY else Side.BUY
