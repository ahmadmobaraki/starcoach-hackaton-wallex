from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import yaml
from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from wallex_trader.tradability import blocked_markets

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"
MIN_NOTIONAL_FLOOR_TMN = 100_000.0


def _find_project_root() -> Path:
    for candidate in (Path.cwd(), *Path.cwd().parents):
        if (candidate / "pyproject.toml").exists() or (candidate / "configs" / "base.yaml").exists():
            return candidate
    app_root = Path("/app")
    if (app_root / "configs" / "base.yaml").exists():
        return app_root
    return Path.home() / ".argus"


PROJECT_ROOT = _find_project_root()


class Mode(StrEnum):
    DOWNLOAD = "download"
    BACKTEST = "backtest"
    SHADOW = "shadow"
    LIVE = "live"
    REPLAY = "replay"


class Settings(BaseSettings):
    """Runtime settings.

    Precedence: real environment > .env > mode YAML > field defaults.
    Keys and other secrets are never included in logs or dashboards.
    """

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_ignore_empty=True,
    )

    # ---- runtime -------------------------------------------------------
    mode: Mode = Mode.SHADOW
    log_level: str = "INFO"
    run_id: str | None = None
    timezone: str = "Asia/Tehran"
    git_sha: str | None = None
    heartbeat_sec: int = 15

    # ---- challenge window ---------------------------------------------
    challenge_start: datetime | None = None
    challenge_end: datetime | None = None

    # ---- Wallex --------------------------------------------------------
    wallex_api_base: str = "https://api.wallex.ir"
    wallex_ws_url: str = "wss://api.wallex.ir/connection/websocket"
    wallex_mcp_url: str = "https://api.wallex.ir/mcp"
    wallex_api_key: SecretStr = SecretStr("")
    wallex_sub_account_client_id: str | None = None
    wallex_public_rate_limit: float = 3.0
    wallex_order_rate_limit: float = 1.5

    # ---- DeepSeek ------------------------------------------------------
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-flash"
    deepseek_deep_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_reasoning_effort: str = "low"
    deepseek_deep_reasoning_effort: str = "high"

    # ---- storage -------------------------------------------------------
    postgres_dsn: str = "sqlite+aiosqlite:///data/argus.db"
    data_dir: Path = PROJECT_ROOT / "data"
    artifacts_dir: Path = PROJECT_ROOT / "artifacts"
    logs_dir: Path = PROJECT_ROOT / "logs"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    run_state_dir: Path = PROJECT_ROOT / "data" / "runs"
    journal_path: Path = PROJECT_ROOT / "artifacts" / "agent_journal.md"

    # ---- dashboard -----------------------------------------------------
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    dashboard_basic_auth_user: str = "admin"
    dashboard_basic_auth_password: SecretStr = SecretStr("")
    dashboard_kill_token: SecretStr = SecretStr("")

    # ---- universe ------------------------------------------------------
    universe_allowlist: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "BTCTMN",
            "ETHTMN",
            "SOLTMN",
            "XRPTMN",
            "DOGETMN",
            "PEPETMN",
            "USDTTMN",
            "XAUTTMN",
        ]
    )
    universe_max_symbols: int = 8
    universe_min_quote_volume_24h: float = 5_000_000_000
    universe_min_listing_age_days: int = 14
    universe_refresh_sec: int = 3600
    blocked_markets_file: Path = PROJECT_ROOT / "configs" / "blocked_markets.txt"
    max_spread_bps: float = 35.0
    max_depth_age_ms: int = 5000
    max_tick_age_ms: int = 10000

    # ---- strategy ------------------------------------------------------
    strategy_mode: str = "swing"  # swing | intraday | both
    intraday_timeframe_minutes: int = 1
    intraday_risk_multiplier: float = 0.25
    candidate_threshold: float = 0.65
    alpha_threshold_bps: float = 100.0
    z_entry: float = 2.2
    min_expected_move_bps: float = 80.0
    cost_buffer_bps: float = 12.0
    max_candidates_per_cycle: int = 3
    decision_interval_sec: float = 5.0
    position_interval_sec: float = 1.0
    regime_memo_interval_sec: int = 1800
    max_spread_to_trade_bps: float = 50.0
    max_volatility_percentile: float = 99.0

    # ---- risk ----------------------------------------------------------
    initial_capital_tmn: float = 5_000_000.0
    risk_per_trade: float = 0.0075
    risk_aversion: float = 0.25
    max_position_pct: float = 0.20
    max_gross_exposure: float = 0.60
    max_portfolio_heat: float = 0.03
    de_risk_drawdown_pct: float = 0.03
    hard_de_risk_drawdown_pct: float = 0.045
    max_open_positions: int = 4
    max_correlated_exposure: float = 0.35
    min_cash_buffer: float = 0.25
    daily_loss_limit: float = 0.025
    max_drawdown: float = 0.06
    max_consecutive_losses: int = 2
    losing_streak_cooldown_sec: int = 900
    stop_loss_bps: float = 100.0
    take_profit_bps: float = 180.0
    time_stop_sec: int = 1200
    trail_trigger_bps: float = 120.0
    trail_distance_bps: float = 70.0
    min_stop_bps: float = 40.0
    max_stop_bps: float = 1200.0
    min_take_profit_bps: float = 60.0
    max_take_profit_bps: float = 3000.0
    min_confidence: float = 0.55
    max_slippage_bps: float = 25.0
    max_depth_fraction: float = 0.10
    min_notional_tmn: float = MIN_NOTIONAL_FLOOR_TMN
    max_order_notional_tmn: float = 750_000.0

    # ---- execution -----------------------------------------------------
    maker_fee: float = 0.0025
    taker_fee: float = 0.0030
    order_rate_limit_per_10s: int = 15
    max_open_orders: int = 12
    order_timeout_sec: int = 20
    max_reprices: int = 3
    min_order_lifetime_sec: int = 2
    flatten_on_kill: bool = True
    flatten_on_stop: bool = False
    live_order_dry_run: bool = False
    flatten_at_end: bool = True
    ws_flatten_after_sec: int = 60
    ws_down_kill_sec: int = 30
    clock_skew_kill_sec: float = 5.0
    no_new_entries_before_end_sec: int = 7200
    flatten_before_end_sec: int = 1800

    # ---- agent ---------------------------------------------------------
    agent_enabled: bool = True
    agent_min_confidence: float = 0.55
    agent_min_size_fraction: float = 0.7
    agent_max_calls_per_day: int = 2000
    agent_timeout_sec: float = 20.0
    agent_temperature: float = 0.0
    agent_max_concurrent: int = 3
    agent_symbol_cooldown_sec: int = 30
    agent_max_tokens: int = 1400
    allow_agent_fallback: bool = False
    agent_escalation_enabled: bool = True
    agent_reflection_enabled: bool = True
    agent_reflection_model: str | None = None
    agent_reflection_min_interval_sec: int = 20
    agent_review_trend_positions: bool = False
    agent_session_review_interval_sec: int = 7200
    agent_notes_in_prompt: int = 6

    # ---- backtest ------------------------------------------------------
    backtest_start: str = ""
    backtest_end: str = ""
    backtest_resolution: str = "1"
    backtest_slippage_bps: float = 8.0
    backtest_latency_ms: int = 250
    backtest_initial_capital_tmn: float = 5_000_000.0
    backtest_maker_fee: float = 0.0025
    backtest_taker_fee: float = 0.0030
    backtest_agent_mode: str = "deterministic"  # deterministic | ai | cached
    backtest_cache_file: str = ""
    backtest_decision_interval_sec: int = 300

    # ---- slow trend strategy -------------------------------------------
    swing_fast_ema: int = 48
    swing_slow_ema: int = 240
    swing_momentum_bars: int = 6
    swing_stop_bps: float = 1000.0
    swing_take_profit_bps: float = 2500.0
    swing_horizon_sec: int = 1_728_000
    swing_trail_trigger_bps: float = 800.0
    swing_trail_distance_bps: float = 800.0
    swing_min_score: float = 0.60
    swing_prefer_marketable: bool = True
    swing_btc_filter: bool = False
    swing_timeframe_hours: int = 4
    swing_history_days: int = 60

    # ---- live safety ---------------------------------------------------
    live_confirm: str = ""

    @field_validator("universe_allowlist", mode="before")
    @classmethod
    def _split_symbols(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [s.strip().upper() for s in value.split(",") if s.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(s).strip().upper() for s in value if str(s).strip()]
        return value

    @field_validator("challenge_start", "challenge_end", mode="before")
    @classmethod
    def _empty_datetime(cls, value: Any) -> Any:
        if value in ("", None):
            return None
        return value

    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.mode == Mode.LIVE:
            if self.live_confirm != LIVE_CONFIRM_PHRASE:
                raise ValueError(
                    "MODE=live requires LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY"
                )
            if not self.wallex_api_key.get_secret_value():
                raise ValueError("MODE=live requires WALLEX_API_KEY")
            if self.live_order_dry_run:
                raise ValueError("MODE=live cannot use LIVE_ORDER_DRY_RUN")
            if self.max_order_notional_tmn > 250_000:
                raise ValueError("live canary requires MAX_ORDER_NOTIONAL_TMN <= 250000")
            if self.max_position_pct > 0.10:
                raise ValueError("live canary requires MAX_POSITION_PCT <= 0.10")
            if self.risk_per_trade > 0.005:
                raise ValueError("live canary requires RISK_PER_TRADE <= 0.005")
        if self.mode != Mode.LIVE and self.live_confirm:
            raise ValueError("LIVE_CONFIRM must be empty unless MODE=live")
        if self.mode in (Mode.LIVE, Mode.SHADOW):
            conflicts = sorted(
                set(self.universe_allowlist) & blocked_markets(self.blocked_markets_file)
            )
            if conflicts:
                raise ValueError(
                    "UNIVERSE_ALLOWLIST contains markets not tradable in this account: "
                    + ", ".join(conflicts)
                )
        if not self.universe_allowlist:
            raise ValueError("UNIVERSE_ALLOWLIST cannot be empty")
        if self.max_position_pct > self.max_gross_exposure:
            raise ValueError("MAX_POSITION_PCT must be <= MAX_GROSS_EXPOSURE")
        if self.risk_per_trade <= 0 or self.risk_per_trade > 0.05:
            raise ValueError("RISK_PER_TRADE must be in (0, 0.05]")
        if self.daily_loss_limit <= 0 or self.daily_loss_limit > 0.10:
            raise ValueError("DAILY_LOSS_LIMIT must be in (0, 0.10]")
        if self.max_drawdown <= 0 or self.max_drawdown > 0.25:
            raise ValueError("MAX_DRAWDOWN must be in (0, 0.25]")
        if self.stop_loss_bps <= 0 or self.take_profit_bps <= 0:
            raise ValueError("stop/take-profit must be positive")
        if self.min_notional_tmn < MIN_NOTIONAL_FLOOR_TMN:
            raise ValueError(
                f"MIN_NOTIONAL_TMN must be >= {MIN_NOTIONAL_FLOOR_TMN:.0f}"
            )
        if self.min_notional_tmn > self.max_order_notional_tmn:
            raise ValueError("MIN_NOTIONAL_TMN must be <= MAX_ORDER_NOTIONAL_TMN")
        if not 0 <= self.maker_fee < 0.05 or not 0 <= self.taker_fee < 0.05:
            raise ValueError("fees must be in [0, 0.05)")
        if not 1 <= self.swing_timeframe_hours <= 24:
            raise ValueError("SWING_TIMEFRAME_HOURS must be between 1 and 24")
        if not 1 <= self.intraday_timeframe_minutes <= 15:
            raise ValueError("INTRADAY_TIMEFRAME_MINUTES must be between 1 and 15")
        if self.challenge_start and self.challenge_end:
            if self.challenge_end <= self.challenge_start:
                raise ValueError("CHALLENGE_END must be after CHALLENGE_START")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Environment always wins over YAML/defaults.
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)

    @property
    def has_wallex_key(self) -> bool:
        return bool(self.wallex_api_key.get_secret_value())

    @property
    def has_deepseek_key(self) -> bool:
        return bool(self.deepseek_api_key.get_secret_value())

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL_SWITCH"

    @property
    def is_live(self) -> bool:
        return self.mode == Mode.LIVE

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.artifacts_dir,
            self.logs_dir,
            self.cache_dir,
            self.run_state_dir,
            self.data_dir / "raw",
            self.data_dir / "curated",
            self.data_dir / "manifests",
            self.data_dir / "cache" / "llm",
            self.journal_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def safe_dump(self, *, redact: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                data[name] = "***" if redact and value.get_secret_value() else ""
            elif isinstance(value, Path):
                data[name] = str(value)
            elif isinstance(value, datetime):
                data[name] = value.isoformat()
            elif isinstance(value, StrEnum):
                data[name] = value.value
            else:
                data[name] = value
        return data

    def config_hash(self) -> str:
        payload = json.dumps(self.safe_dump(redact=True), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def universe_or_default(self) -> list[str]:
        return list(dict.fromkeys(self.universe_allowlist))


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config {path} must contain a YAML mapping")
    return data


def _detect_mode(explicit: str | None, config_dir: Path) -> str:
    if explicit:
        return explicit.lower()
    env_mode = os.environ.get("MODE")
    if env_mode:
        return env_mode.lower()
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        values = dotenv_values(env_file)
        if values.get("MODE"):
            return str(values["MODE"]).lower()
    base = _read_yaml(config_dir / "base.yaml")
    return str(base.get("mode", "shadow")).lower()


def load_settings(
    mode: str | None = None,
    config_dir: str | Path | None = None,
) -> Settings:
    """Load YAML defaults for the selected mode, then let environment override."""
    cfg_dir = Path(config_dir) if config_dir else PROJECT_ROOT / "configs"
    selected = _detect_mode(mode, cfg_dir)
    base = _read_yaml(cfg_dir / "base.yaml")
    overlay = _read_yaml(cfg_dir / f"{selected}.yaml")
    values = _deep_merge(base, overlay)
    values["mode"] = selected
    # Environment has priority over YAML for every field. Force MODE to the
    # explicit CLI mode for the duration of construction, then restore it.
    previous_mode = os.environ.get("MODE")
    os.environ["MODE"] = selected
    try:
        settings = Settings(**values)
    finally:
        if previous_mode is None:
            os.environ.pop("MODE", None)
        else:
            os.environ["MODE"] = previous_mode
    settings.ensure_dirs()
    if settings.git_sha is None:
        settings.git_sha = _git_sha()
    return settings


def _git_sha() -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except OSError:
        return "no-git"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "no-git"
