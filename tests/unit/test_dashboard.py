from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from wallex_trader.dashboard.api import create_app
from wallex_trader.models import PortfolioSnapshot
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository


def test_dashboard_renders_empty_state(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        response = client.get("/test-results")
        assert response.status_code == 200
        assert "waiting for a trader run" in response.text
        response = client.get("/test-results/decisions")
        assert response.status_code == 200
        response = client.get("/test-results/memory")
        assert response.status_code == 200


def test_dashboard_renders_run(settings) -> None:
    async def seed() -> None:
        db = create_database(settings)
        await db.init()
        repo = Repository(db, "test-run")
        await repo.start_run(settings, "test-run")
        snapshot = PortfolioSnapshot(
            ts=datetime.now(UTC),
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
        await repo.save_equity(snapshot)
        await repo.heartbeat("trader", {"ws": {"ws_connected": True}})
        await repo.save_decision(
            {
                "decision_id": "dec-1",
                "ts": datetime.now(UTC),
                "symbol": "BTCTMN",
                "action": "HOLD",
                "confidence": 0.5,
                "signal": "MR",
                "score": 0.7,
                "candidate": {},
                "features": {},
                "agent": {"model": "deepseek-flash"},
                "risk": {"approved": True, "checks": []},
                "execution": {"status": "NO_TRADE"},
                "rationale": "test hold",
                "prompt_hash": "p",
                "response_hash": "r",
            }
        )
        await db.close()

    import asyncio

    asyncio.run(seed())
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/test-results")
        assert response.status_code == 200
        assert "test-run" in response.text
        response = client.get("/api/overview")
        assert response.status_code == 200
        payload = response.json()
        assert payload["equity"]["equity_tmn"] == "5000000"
        response = client.get("/test-results/decisions")
        assert "test hold" in response.text


def test_live_badge_blinks_while_run_is_running(settings) -> None:
    from datetime import datetime

    from wallex_trader.storage.db import create_database
    from wallex_trader.storage.models import RunRow

    async def seed_live() -> None:
        db = create_database(settings)
        await db.init()
        async with db.session() as session:
            session.add(
                RunRow(
                    run_id="live-test",
                    mode="live",
                    git_sha="no-git",
                    config_hash="x",
                    config_json={},
                    status="running",
                    started_at=datetime.now(UTC),
                    ended_at=None,
                    metadata_json={},
                )
            )
        await db.close()

    import asyncio

    asyncio.run(seed_live())
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/test-results")
        assert response.status_code == 200
        assert "live-dot" in response.text
        assert "LIVE" in response.text
