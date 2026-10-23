from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from wallex_trader.models import Fill, Side
from wallex_trader.strategy.portfolio import Portfolio, PortfolioError


def test_buy_sell_accounting(settings) -> None:
    portfolio = Portfolio(settings, Decimal("5000000"))
    now = datetime.now(UTC)
    buy = Fill(
        fill_id="f1",
        order_id="o1",
        symbol="BTCTMN",
        side=Side.BUY,
        price=Decimal("100000"),
        quantity=Decimal("1"),
        fee=Decimal("300"),
        fee_asset="TMN",
        ts=now,
        decision_id="d1",
    )
    portfolio.apply_fill(
        buy,
        entry_meta={
            "decision_id": "d1",
            "signal": "MR",
            "confidence": 0.7,
            "stop_bps": 100,
            "take_profit_bps": 200,
            "horizon_sec": 600,
            "features": {"x": 1},
        },
    )
    assert portfolio.cash_tmn == Decimal("4899700")
    assert portfolio.equity_tmn() == Decimal("4999700")
    assert portfolio.positions["BTCTMN"].stop_price == Decimal("99000.00")
    assert portfolio.positions["BTCTMN"].take_profit_price == Decimal("102000.00")

    sell = Fill(
        fill_id="f2",
        order_id="o2",
        symbol="BTCTMN",
        side=Side.SELL,
        price=Decimal("101000"),
        quantity=Decimal("1"),
        fee=Decimal("303"),
        fee_asset="TMN",
        ts=now + timedelta(seconds=30),
        decision_id="d2",
    )
    trade = portfolio.apply_fill(sell, exit_reason="take_profit")
    assert trade is not None
    assert trade["pnl_tmn"] == "397"
    assert portfolio.cash_tmn == Decimal("5000397")
    assert portfolio.realized_pnl_tmn == Decimal("397")
    assert portfolio.fees_tmn == Decimal("603")
    assert "BTCTMN" not in portfolio.positions


def test_reservations_do_not_change_equity(settings) -> None:
    portfolio = Portfolio(settings, Decimal("5000000"))
    portfolio.reserve_cash(Decimal("1000000"))
    assert portfolio.equity_tmn() == Decimal("5000000")
    assert portfolio.cash_buffer_pct() == 0.8
    portfolio.release_cash(Decimal("1000000"))
    assert portfolio.cash_buffer_pct() == 1.0


def test_insufficient_cash(settings) -> None:
    portfolio = Portfolio(settings, Decimal("100000"))
    fill = Fill(
        fill_id="f1",
        order_id="o1",
        symbol="BTCTMN",
        side=Side.BUY,
        price=Decimal("100000"),
        quantity=Decimal("2"),
        fee=Decimal("600"),
        fee_asset="TMN",
        ts=datetime.now(UTC),
    )
    with pytest.raises(PortfolioError):
        portfolio.apply_fill(fill, entry_meta={})


def test_drawdown(settings) -> None:
    portfolio = Portfolio(settings, Decimal("1000000"))
    snapshot = portfolio.snapshot()
    assert snapshot.drawdown_pct == 0.0
    portfolio.cash_tmn = Decimal("900000")
    snapshot = portfolio.snapshot()
    assert snapshot.drawdown_pct == pytest.approx(0.1)
