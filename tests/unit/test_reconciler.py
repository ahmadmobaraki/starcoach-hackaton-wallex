from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from wallex_trader.execution.dust import add_dust, is_sub_step, is_unsellable, set_dust
from wallex_trader.execution.reconciler import Reconciler
from wallex_trader.models import Position
from wallex_trader.strategy.portfolio import Portfolio


class FakeBroker:
    def __init__(self, balances: dict[str, dict[str, str]], mismatch: bool = False) -> None:
        self._balances = balances
        self._mismatch = mismatch

    async def reconcile(self) -> dict[str, object]:
        return {"mismatch": self._mismatch, "orders": {}}

    async def balances(self) -> dict[str, dict[str, str]]:
        return self._balances


def make_position(quantity: Decimal) -> Position:
    return Position(
        symbol="BNBTMN",
        quantity=quantity,
        avg_entry=Decimal("205115216"),
        stop_price=Decimal("184049883"),
        take_profit_price=Decimal("255624838"),
        opened_at=datetime.now(UTC),
        horizon_sec=1_728_000,
        decision_id="test",
    )


@pytest.mark.asyncio
async def test_reconciler_subtracts_tracked_dust(settings) -> None:
    portfolio = Portfolio(settings, Decimal("4990623"))
    portfolio.positions["BNBTMN"] = make_position(Decimal("0.00000727"))
    broker = FakeBroker(
        {
            "TMN": {"value": "4990623"},
            "BNB": {"value": "0.00002346"},
        }
    )
    reconciler = Reconciler(broker, portfolio, dust={"BNB": Decimal("0.00001619")})
    result = await reconciler.check()
    assert result["ok"] is True
    position_check = next(c for c in result["checks"] if c["name"] == "position_BNBTMN")
    assert position_check["adjusted_balance"] == "0.00000727"
    assert position_check["dust"] == "0.00001619"


@pytest.mark.asyncio
async def test_reconciler_flags_unexplained_excess_without_dust(settings) -> None:
    portfolio = Portfolio(settings, Decimal("4990623"))
    portfolio.positions["BNBTMN"] = make_position(Decimal("0.00000727"))
    broker = FakeBroker(
        {
            "TMN": {"value": "4990623"},
            "BNB": {"value": "0.00002346"},
        }
    )
    reconciler = Reconciler(broker, portfolio)
    result = await reconciler.check()
    assert result["ok"] is False
    position_check = next(c for c in result["checks"] if c["name"] == "position_BNBTMN")
    assert position_check["difference"] == "0.00001619"


@pytest.mark.asyncio
async def test_reconciler_still_flags_cash_mismatch(settings) -> None:
    portfolio = Portfolio(settings, Decimal("100"))
    broker = FakeBroker({"TMN": {"value": "50"}})
    reconciler = Reconciler(broker, portfolio)
    result = await reconciler.check()
    assert result["ok"] is False
    cash = next(c for c in result["checks"] if c["name"] == "tmn_balance")
    assert cash["difference"] == "50"


def test_sub_step_and_unsellable_helpers() -> None:
    assert is_sub_step(Decimal("0.00000727"), Decimal("0.00001")) is True
    assert is_sub_step(Decimal("0.00001"), Decimal("0.00001")) is False
    assert is_unsellable(Decimal("0.00001619"), Decimal("0.00001"), Decimal("204000000"), Decimal("50000")) is True
    assert is_unsellable(Decimal("0.001"), Decimal("0.00001"), Decimal("204000000"), Decimal("50000")) is False


def test_dust_ledger_accumulates_and_sets() -> None:
    dust: dict[str, Decimal] = {}
    assert add_dust(dust, "BNB", Decimal("0.00001619")) == Decimal("0.00001619")
    assert add_dust(dust, "BNB", Decimal("0.00000727")) == Decimal("0.00002346")
    set_dust(dust, "BNB", Decimal("0.5"))
    assert dust["BNB"] == Decimal("0.5")
    set_dust(dust, "BNB", Decimal("0"))
    assert "BNB" not in dust
