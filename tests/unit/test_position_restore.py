from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import update

from wallex_trader.models import PortfolioSnapshot, Position
from wallex_trader.storage.db import create_database
from wallex_trader.storage.models import RunRow
from wallex_trader.storage.repositories import Repository
from wallex_trader.strategy.portfolio import Portfolio


async def mark_live(db, run_ids: list[str]) -> None:
    async with db.session() as session:
        await session.execute(
            update(RunRow).where(RunRow.run_id.in_(run_ids)).values(mode="live")
        )
        await session.commit()


def snapshot(now: datetime | None = None) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        ts=now or datetime.now(UTC),
        cash_tmn=Decimal("5000000"),
        positions_value_tmn=Decimal("0"),
        equity_tmn=Decimal("5000000"),
        realized_pnl_tmn=Decimal("0"),
        unrealized_pnl_tmn=Decimal("0"),
        fees_tmn=Decimal("0"),
        gross_exposure_pct=0,
        cash_buffer_pct=1,
        open_positions=0,
        portfolio_heat_pct=0,
        peak_equity_tmn=Decimal("5000000"),
        drawdown_pct=0,
        daily_pnl_pct=0,
        net_return_pct=0,
    )


def make_position(symbol: str = "BNBTMN", quantity: Decimal = Decimal("0.00091")) -> Position:
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_entry=Decimal("204310340"),
        stop_price=Decimal("184049883"),
        take_profit_price=Decimal("255624838"),
        opened_at=datetime.now(UTC),
        horizon_sec=1_728_000,
        decision_id="dec-test",
    )


def portfolio_with(position: Position, settings) -> Portfolio:
    portfolio = Portfolio(settings, Decimal("5000000"))
    portfolio.positions[position.symbol] = position
    portfolio.marks[position.symbol] = position.avg_entry
    return portfolio


@pytest.mark.asyncio
async def test_tombstone_prevents_restore_resurrection(settings) -> None:
    db = create_database(settings)
    await db.init()
    repo_a = Repository(db, "restore-test-run-a")
    await repo_a.start_run(settings, "restore-test-run-a")
    repo_b = Repository(db, "restore-test-run-b")
    await repo_b.start_run(settings, "restore-test-run-b")
    await mark_live(db, ["restore-test-run-a", "restore-test-run-b"])

    # Run A holds a position and writes snapshot rows while it is open.
    open_portfolio = portfolio_with(make_position(), settings)
    await repo_a.save_position_snapshot(snapshot(), open_portfolio)

    # Run B starts after a shutdown flatten: the position is closed and the
    # final snapshot writes a tombstone for it.
    await repo_b.save_position_snapshot(snapshot(), Portfolio(settings, Decimal("5000000")), closed_symbols=["BNBTMN"])

    rows = await repo_b.latest_positions_for_mode("live")
    owned = {row["symbol"]: row for row in rows}
    assert owned["BNBTMN"]["quantity"] == "0"

    # Restore must not resurrect the closed position.
    restored = Portfolio(settings, Decimal("5000000"))
    restored.restore_positions(rows)
    assert restored.positions == {}

    # The dashboard position list hides tombstones entirely.
    display = await repo_b.latest_positions("restore-test-run-b")
    assert display == []
    await db.close()


@pytest.mark.asyncio
async def test_open_position_restores_from_latest_row(settings) -> None:
    db = create_database(settings)
    await db.init()
    repo_a = Repository(db, "restore-open-run-a")
    await repo_a.start_run(settings, "restore-open-run-a")
    repo_b = Repository(db, "restore-open-run-b")
    await repo_b.start_run(settings, "restore-open-run-b")
    await mark_live(db, ["restore-open-run-a", "restore-open-run-b"])

    position = make_position("BTCTMN", Decimal("0.001"))
    await repo_a.save_position_snapshot(snapshot(), portfolio_with(position, settings))

    # Run B restores the still-open position and keeps reporting it.
    await repo_b.save_position_snapshot(snapshot(), portfolio_with(position, settings))

    rows = await repo_b.latest_positions_for_mode("live")
    restored = Portfolio(settings, Decimal("5000000"))
    restored.restore_positions(rows)
    assert restored.positions["BTCTMN"].quantity == Decimal("0.001")

    display = await repo_b.latest_positions("restore-open-run-b")
    assert display[0]["symbol"] == "BTCTMN"
    assert display[0]["quantity"] == "0.001"
    await db.close()
