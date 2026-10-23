from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from wallex_trader.config import Settings
from wallex_trader.models import MarketMeta
from wallex_trader.strategy.universe import UniverseSelector
from wallex_trader.tradability import blocked_markets, is_blocked, normalize_market


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "mode": "shadow",
        "data_dir": tmp_path / "data",
        "artifacts_dir": tmp_path / "artifacts",
        "logs_dir": tmp_path / "logs",
        "cache_dir": tmp_path / "data" / "cache",
        "run_state_dir": tmp_path / "data" / "runs",
        "journal_path": tmp_path / "artifacts" / "agent_journal.md",
        "postgres_dsn": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        "dashboard_basic_auth_password": "",
        "dashboard_kill_token": "test-kill-token",
        "agent_enabled": False,
        "agent_reflection_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_normalize_market() -> None:
    assert normalize_market("ZEC/TMN") == "ZECTMN"
    assert normalize_market("1bbabydoge/usdt") == "1BBABYDOGEUSDT"
    assert normalize_market(" btctmn ") == "BTCTMN"


def test_operator_blocklist_is_loaded() -> None:
    blocked = blocked_markets()
    assert "ZECTMN" in blocked
    assert "ZECUSDT" in blocked
    assert "APTTMN" in blocked
    assert "1BBABYDOGETMN" in blocked
    assert "BTCTMN" not in blocked
    assert "PEPETMN" not in blocked
    assert not is_blocked("BTCTMN")


def test_live_and_shadow_refuse_blocked_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="not tradable in this account"):
        make_settings(tmp_path, universe_allowlist="BTCTMN,ZECTMN")
    with pytest.raises(ValidationError, match="not tradable in this account"):
        make_settings(
            tmp_path,
            mode="live",
            live_confirm="I_UNDERSTAND_REAL_MONEY",
            wallex_api_key="test-key",
            max_order_notional_tmn=250_000,
            max_position_pct=0.10,
            risk_per_trade=0.005,
            universe_allowlist="ZECTMN",
        )


def test_tradable_allowlist_is_accepted(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, universe_allowlist="BTCTMN,PEPETMN")
    assert settings.universe_or_default() == ["BTCTMN", "PEPETMN"]


def test_universe_selector_excludes_blocked_symbols(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, universe_allowlist="BTCTMN")
    settings.universe_allowlist = ["BTCTMN", "ZECTMN"]
    selector = UniverseSelector(settings)
    metadata = {
        "BTCTMN": MarketMeta(
            symbol="BTCTMN",
            base_asset="BTC",
            quote_asset="TMN",
            price_precision=0,
            amount_precision=8,
            price=100_000.0,
            quote_volume_24h=20_000_000_000,
        ),
        "ZECTMN": MarketMeta(
            symbol="ZECTMN",
            base_asset="ZEC",
            quote_asset="TMN",
            price_precision=0,
            amount_precision=4,
            price=300_000.0,
            quote_volume_24h=20_000_000_000,
        ),
    }
    result = selector.select(metadata)
    assert [item.symbol for item in result.selected] == ["BTCTMN"]
    assert result.excluded["ZECTMN"] == "not tradable in this account"


def test_min_notional_floor_cannot_be_lowered(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="MIN_NOTIONAL_TMN must be >="):
        make_settings(tmp_path, min_notional_tmn=50_000)
