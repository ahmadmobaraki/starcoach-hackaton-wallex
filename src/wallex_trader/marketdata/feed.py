from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.exchange.rest import WallexAPIError, WallexRESTClient
from wallex_trader.exchange.ws import WallexWebSocket
from wallex_trader.marketdata.candles import CandleStore
from wallex_trader.marketdata.orderbook import BookStore
from wallex_trader.marketdata.tape import TradeTape
from wallex_trader.models import Candle, OrderBook, TradeTick
from wallex_trader.strategy.swing import resample_candles

logger = logging.getLogger("trader.feed")


class MarketDataFeed:
    def __init__(
        self,
        settings: Settings,
        rest: WallexRESTClient,
        symbols: list[str],
        *,
        books: BookStore | None = None,
        candles: CandleStore | None = None,
        tape: TradeTape | None = None,
    ) -> None:
        self.settings = settings
        self.rest = rest
        self.symbols = list(dict.fromkeys(symbols))
        self.books = books or BookStore()
        self.candles = candles or CandleStore()
        self.hourly = CandleStore(maxlen=12000)
        self.tape = tape or TradeTape()
        self.last_hourly_sync: dict[str, datetime] = {}
        self.prices: dict[str, Decimal] = {}
        self.ws: WallexWebSocket | None = None
        self.last_rest_sync: datetime | None = None
        self.last_depth_sync: dict[str, datetime] = {}
        self.last_trade_sync: dict[str, datetime] = {}
        self.last_candle_sync: dict[str, datetime] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._rest_errors = 0
        self._rest_calls = 0

    async def start(self, *, seed_days: int = 2) -> None:
        await self.seed(seed_days=seed_days)
        self.ws = WallexWebSocket(
            self.settings,
            self._channels(),
            self._on_message,
            on_connect=self._on_ws_connect,
            on_disconnect=self._on_ws_disconnect,
        )
        await self.ws.start()
        self._tasks = [
            asyncio.create_task(self._rest_fallback_loop(), name="feed-rest-fallback"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            await self.ws.stop()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()

    async def seed(self, *, seed_days: int = 2) -> None:
        for symbol in self.symbols:
            await self._sync_depth(symbol)
            await self._sync_trades(symbol)
            await self._sync_candles(symbol, days=seed_days)
            if self.settings.strategy_mode in ("swing", "both"):
                await self._sync_hourly_history(
                    symbol, days=self._swing_history_days()
                )

    async def update_symbols(self, symbols: list[str]) -> None:
        new_symbols = [symbol for symbol in dict.fromkeys(symbols) if symbol]
        if set(new_symbols) == set(self.symbols):
            return
        logger.info("universe changed: %s -> %s", self.symbols, new_symbols)
        self.symbols = new_symbols
        if self.ws is not None:
            await self.ws.stop()
            self.ws = WallexWebSocket(
                self.settings,
                self._channels(),
                self._on_message,
                on_connect=self._on_ws_connect,
                on_disconnect=self._on_ws_disconnect,
            )
            await self.ws.start()
        for symbol in new_symbols:
            if self.books.get(symbol) is None:
                await self._sync_depth(symbol)
            if not self.candles.candles(symbol):
                await self._sync_candles(symbol, days=2)
            if not self.tape.recent(symbol, 300):
                await self._sync_trades(symbol)
            if (
                self.settings.strategy_mode in ("swing", "both")
                and not self.hourly.candles(symbol)
            ):
                await self._sync_hourly_history(
                    symbol, days=self._swing_history_days()
                )

    def _channels(self) -> list[str]:
        channels: list[str] = ["public:all@price"]
        for symbol in self.symbols:
            channels.extend(
                [
                    f"orderbook:{symbol}@buyDepth",
                    f"orderbook:{symbol}@sellDepth",
                    f"public:{symbol}@trade",
                    f"candle:{symbol}@1m",
                ]
            )
        return channels

    async def _on_ws_connect(self) -> None:
        logger.info("resyncing market data after websocket connect")
        await self._resync_all()

    async def _on_ws_disconnect(self) -> None:
        logger.warning("websocket disconnected; REST fallback remains active")

    async def _resync_all(self) -> None:
        for symbol in self.symbols:
            try:
                await self._sync_depth(symbol)
                await self._sync_trades(symbol)
                if self.candles.age_ms(symbol) > 180_000:
                    await self._sync_candles(symbol, days=1)
            except WallexAPIError as exc:
                self._rest_errors += 1
                logger.warning("resync failed for %s: %s", symbol, exc)

    async def _on_message(self, channel: str, payload: Any) -> None:
        if channel == "public:all@price":
            data = payload.get("data", payload) if isinstance(payload, dict) else {}
            symbol = str(data.get("symbol", "")).upper()
            if symbol in self.symbols:
                try:
                    self.prices[symbol] = Decimal(str(data.get("price", 0)))
                except (TypeError, ValueError):
                    pass
            return
        if channel.startswith("orderbook:"):
            parts = channel.split(":", 1)[1].split("@")
            symbol = parts[0].upper()
            self.books.update_from_ws(symbol, channel, payload)
            self.last_depth_sync[symbol] = datetime.now(UTC)
            return
        if channel.startswith("public:") and channel.endswith("@trade"):
            symbol = channel[len("public:") : -len("@trade")].upper()
            if symbol in self.symbols:
                parsed = self.tape.parse_payload(symbol, payload)
                if parsed:
                    self.last_trade_sync[symbol] = datetime.now(UTC)
            return
        if channel.startswith("candle:") and channel.endswith("@1m"):
            symbol = channel[len("candle:") : -len("@1m")].upper()
            data = payload
            if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                data = payload["data"]
            self.candles.upsert_udf(symbol, data)
            self.last_candle_sync[symbol] = datetime.now(UTC)

    async def _rest_fallback_loop(self) -> None:
        cycle = 0
        while not self._stop.is_set():
            try:
                await asyncio.sleep(5)
                cycle += 1
                now = datetime.now(UTC)
                ws_ok = bool(self.ws and self.ws.connected)
                stale_books = [
                    symbol
                    for symbol in self.symbols
                    if not self.books.is_fresh(symbol, 8_000)
                ]
                for symbol in stale_books[:4]:
                    await self._sync_depth(symbol)
                quiet_symbols = [
                    symbol
                    for symbol in self.symbols
                    if not self.tape.recent(symbol, 90, now)
                ]
                for symbol in quiet_symbols[:2]:
                    await self._sync_trades(symbol)
                if not ws_ok or cycle % 12 == 0:
                    for symbol in self.symbols:
                        if self.candles.age_ms(symbol) > 180_000:
                            await self._sync_candles(symbol, days=1)
                if (
                    self.settings.strategy_mode in ("swing", "both")
                    and cycle % 60 == 0
                ):
                    for symbol in self.symbols:
                        await self._sync_hourly_today(symbol)
                self.last_rest_sync = datetime.now(UTC)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._rest_errors += 1
                logger.warning("REST fallback error: %s", exc)

    async def refresh_depth(self, symbol: str) -> None:
        await self._sync_depth(symbol)

    async def refresh_trades(self, symbol: str) -> None:
        await self._sync_trades(symbol)

    async def _sync_depth(self, symbol: str) -> None:
        self._rest_calls += 1
        book = await self.rest.depth(symbol, limit=100)
        self.books.replace(book)
        self.last_depth_sync[symbol] = datetime.now(UTC)
        if book.best_bid is not None and book.best_ask is not None:
            self.prices[symbol] = (book.best_bid + book.best_ask) / Decimal(2)

    async def _sync_trades(self, symbol: str) -> None:
        self._rest_calls += 1
        trades = await self.rest.trades(symbol)
        for trade in trades:
            self.tape.add(trade)
        self.last_trade_sync[symbol] = datetime.now(UTC)

    async def _sync_candles(self, symbol: str, *, days: int = 1) -> None:
        now = datetime.now(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(days, 0, -1):
            start = today - timedelta(days=offset)
            end = start + timedelta(days=1)
            try:
                self._rest_calls += 1
                candles = await self.rest.candles(
                    symbol, resolution="1", start=start, end=min(end, now)
                )
                if candles:
                    self.candles.seed(symbol, candles) if not self.candles.candles(symbol) else None
                    for candle in candles:
                        self.candles.upsert(candle)
            except WallexAPIError as exc:
                self._rest_errors += 1
                logger.warning("candle sync failed for %s: %s", symbol, exc)
        # Also load today's partial history.
        try:
            self._rest_calls += 1
            candles = await self.rest.candles(
                symbol, resolution="1", start=today, end=now
            )
            for candle in candles:
                self.candles.upsert(candle)
        except WallexAPIError as exc:
            self._rest_errors += 1
            logger.warning("candle sync failed for %s: %s", symbol, exc)
        self.last_candle_sync[symbol] = datetime.now(UTC)

    async def _sync_hourly_history(self, symbol: str, *, days: int = 60) -> None:
        now = datetime.now(UTC)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for end_offset in range(0, days, 30):
            end = today - timedelta(days=end_offset)
            start = end - timedelta(days=min(30, days - end_offset))
            try:
                self._rest_calls += 1
                candles = await self.rest.candles(
                    symbol, resolution="60", start=start, end=min(end, now)
                )
                for candle in candles:
                    self.hourly.upsert(candle)
            except WallexAPIError as exc:
                self._rest_errors += 1
                logger.warning("hourly history failed for %s: %s", symbol, exc)
        await self._sync_hourly_today(symbol)

    async def _sync_hourly_today(self, symbol: str) -> None:
        now = datetime.now(UTC)
        start = now - timedelta(days=2)
        try:
            self._rest_calls += 1
            candles = await self.rest.candles(
                symbol, resolution="60", start=start, end=now
            )
            for candle in candles:
                self.hourly.upsert(candle)
            self.last_hourly_sync[symbol] = now
        except WallexAPIError as exc:
            self._rest_errors += 1
            logger.warning("hourly update failed for %s: %s", symbol, exc)

    def _swing_history_days(self) -> int:
        required = int(
            (self.settings.swing_slow_ema + 5)
            * self.settings.swing_timeframe_hours
            / 24
        ) + 10
        return max(self.settings.swing_history_days, required)

    def swing_candles(self, symbol: str, *, now: datetime | None = None) -> list[Candle]:
        return resample_candles(
            self.hourly.candles(symbol),
            hours=self.settings.swing_timeframe_hours,
            now=now,
            drop_incomplete=True,
        )

    def snapshot(self, symbol: str) -> dict[str, Any]:
        book = self.books.get(symbol)
        last_candle = self.candles.last(symbol)
        price = self.prices.get(symbol)
        if price is None and last_candle is not None:
            price = Decimal(str(last_candle.close))
        return {
            "book": book,
            "price": price,
            "last_candle": last_candle,
            "book_age_ms": book.age_ms() if book else None,
        }

    def recent_trades_map(self, seconds: int = 5) -> dict[str, list[TradeTick]]:
        return {symbol: self.tape.recent(symbol, seconds) for symbol in self.symbols}

    def health(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        ws_connected = bool(self.ws and self.ws.connected)
        fresh_books = sum(1 for symbol in self.symbols if self.books.is_fresh(symbol, 8_000))
        candle_ages = [self.candles.age_ms(symbol) for symbol in self.symbols]
        return {
            "ws_connected": ws_connected,
            "ws": self.ws.stats() if self.ws else {},
            "symbols": self.symbols,
            "fresh_books": fresh_books,
            "symbol_count": len(self.symbols),
            "oldest_book_age_ms": round(max(_book_ages(self.books, self.symbols, now), default=0), 1),
            "max_candle_age_ms": round(max(candle_ages, default=0), 1)
            if candle_ages
            else None,
            "last_rest_sync": self.last_rest_sync.isoformat() if self.last_rest_sync else None,
            "hourly_symbols": len([s for s in self.symbols if self.hourly.candles(s)]),
            "hourly_age_ms": round(max((self.hourly.age_ms(s) for s in self.symbols), default=0), 1),
            "rest_calls": self._rest_calls,
            "rest_errors": self._rest_errors,
            "healthy": ws_connected and fresh_books >= max(1, len(self.symbols) // 2),
        }

    def books_map(self) -> dict[str, OrderBook]:
        return {symbol: book for symbol in self.symbols if (book := self.books.get(symbol))}

    def mark_prices(self) -> dict[str, Decimal]:
        result = dict(self.prices)
        for symbol in self.symbols:
            book = self.books.get(symbol)
            if book and book.best_bid is not None:
                result[symbol] = book.best_bid
        return result

    def last_closed_candle(self, symbol: str) -> Candle | None:
        candles = self.candles.candles(symbol, limit=2)
        return candles[-2] if len(candles) >= 2 else (candles[-1] if candles else None)


def _book_ages(books: BookStore, symbols: list[str], now: datetime) -> list[float]:
    ages: list[float] = []
    for symbol in symbols:
        book = books.get(symbol)
        if book is not None:
            ages.append(book.age_ms(now))
    return ages
