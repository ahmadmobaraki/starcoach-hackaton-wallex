from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from wallex_trader.models import BookLevel, OrderBook, Side


def make_book() -> OrderBook:
    return OrderBook(
        symbol="BTCTMN",
        bids=[
            BookLevel(Decimal("100"), Decimal("10")),
            BookLevel(Decimal("99"), Decimal("20")),
        ],
        asks=[
            BookLevel(Decimal("101"), Decimal("5")),
            BookLevel(Decimal("102"), Decimal("15")),
        ],
        updated_at=datetime.now(UTC),
    )


def test_book_metrics() -> None:
    book = make_book()
    assert book.best_bid == Decimal("100")
    assert book.best_ask == Decimal("101")
    assert book.mid == Decimal("100.5")
    assert round(book.spread_bps, 2) == 99.5
    assert book.depth_notional(Side.BUY, 2) == Decimal("2980")
    assert book.depth_notional(Side.SELL, 2) == Decimal("2035")
    assert book.top5_depth_tmn() == 2035.0
    assert 0.1 < book.imbalance(2) < 0.2


def test_staleness() -> None:
    book = make_book()
    assert book.is_healthy(1000)
    book.updated_at = datetime.now(UTC) - timedelta(seconds=10)
    assert not book.is_healthy(1000)
