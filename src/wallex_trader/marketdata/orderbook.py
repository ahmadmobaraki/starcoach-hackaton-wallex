from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from wallex_trader.models import BookLevel, OrderBook, Side


class BookStore:
    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}
        self.updates: dict[str, int] = {}

    def get(self, symbol: str) -> OrderBook | None:
        return self._books.get(symbol)

    def all(self) -> dict[str, OrderBook]:
        return dict(self._books)

    def replace(self, book: OrderBook) -> None:
        self._books[book.symbol] = book
        self.updates[book.symbol] = self.updates.get(book.symbol, 0) + 1

    def update_side(
        self,
        symbol: str,
        side: Side,
        levels: list[BookLevel],
        *,
        source: str = "ws",
    ) -> OrderBook:
        book = self._books.get(symbol) or OrderBook(symbol=symbol)
        if side == Side.BUY:
            book.bids = sorted(levels, key=lambda level: level.price, reverse=True)
        else:
            book.asks = sorted(levels, key=lambda level: level.price)
        book.updated_at = datetime.now(UTC)
        book.source = source
        self._books[symbol] = book
        self.updates[symbol] = self.updates.get(symbol, 0) + 1
        return book

    def update_from_ws(self, symbol: str, channel: str, payload: Any) -> None:
        raw_levels: Any = None
        if isinstance(payload, dict):
            raw_levels = payload.get("message", payload.get("data", payload.get("levels")))
        elif isinstance(payload, list):
            raw_levels = payload
        if not isinstance(raw_levels, list):
            return
        levels: list[BookLevel] = []
        for item in raw_levels:
            try:
                if isinstance(item, dict):
                    levels.append(
                        BookLevel(
                            price=Decimal(str(item["price"])),
                            quantity=Decimal(str(item["quantity"])),
                        )
                    )
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    levels.append(
                        BookLevel(
                            price=Decimal(str(item[0])), quantity=Decimal(str(item[1]))
                        )
                    )
            except (KeyError, TypeError, ValueError):
                continue
        if not levels:
            return
        side = Side.BUY if "buyDepth" in channel else Side.SELL
        self.update_side(symbol, side, levels, source="ws")

    def is_fresh(self, symbol: str, max_age_ms: int) -> bool:
        book = self._books.get(symbol)
        return bool(book and book.is_healthy(max_age_ms))

    def stale_symbols(self, max_age_ms: int) -> list[str]:
        return [symbol for symbol in self._books if not self.is_fresh(symbol, max_age_ms)]
