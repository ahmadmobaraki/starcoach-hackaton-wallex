from __future__ import annotations

from pathlib import Path

import pytest

from wallex_trader.config import Settings


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    values = Settings(
        _env_file=None,
        mode="shadow",
        data_dir=tmp_path / "data",
        artifacts_dir=tmp_path / "artifacts",
        logs_dir=tmp_path / "logs",
        cache_dir=tmp_path / "data" / "cache",
        run_state_dir=tmp_path / "data" / "runs",
        journal_path=tmp_path / "artifacts" / "agent_journal.md",
        postgres_dsn=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        dashboard_basic_auth_password="",
        dashboard_kill_token="test-kill-token",
        agent_enabled=False,
        agent_reflection_enabled=False,
        initial_capital_tmn=5_000_000,
        maker_fee=0.0025,
        taker_fee=0.0030,
        min_notional_tmn=50_000,
        max_order_notional_tmn=750_000,
    )
    values.ensure_dirs()
    return values
