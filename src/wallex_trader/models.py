from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    MARKET = "MARKET"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    PENDING_SEND = "PENDING_SEND"
    SENT = "SENT"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


@dataclass(slots=True)
class MarketMeta:
    symbol: str
    base_asset: str
    quote_asset: str
    price_precision: int
    amount_precision: int
    price: float = 0.0
    quote_volume_24h: float = 0.0
    volume_24h: float = 0.0
    change_24h: float = 0.0
    min_qty: float = 0.0
    min_notional: float = 50_000.0
    tick_size: float = 0.0
    step_size: float = 0.0
    created_at: datetime | None = None
    is_spot: bool = True
    is_tmn_based: bool = True
    is_risky: bool = False
    is_new: bool = False
    flags: list[str] = field(default_factory=list)

    @property
    def tick(self) -> Decimal:
        # Wallex's v1 tickSize/stepSize fields report decimal-place counts for
        # some markets, not increments. Precision is the reliable source.
        return Decimal(1).scaleb(-self.price_precision)

    @property
    def step(self) -> Decimal:
        return Decimal(1).scaleb(-self.amount_precision)

    @property
    def listing_age_days(self) -> float:
        if not self.created_at:
            return 9999.0
        return max(0.0, (utcnow() - self.created_at).total_seconds() / 86400)


@dataclass(slots=True)
class FeeRate:
    maker: float
    taker: float


@dataclass(slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(slots=True)
class OrderBook:
    symbol: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utcnow)
    source: str = "rest"

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / Decimal(2)

    @property
    def spread_bps(self) -> float:
        mid = self.mid
        if mid is None or mid <= 0:
            return 10_000.0
        assert self.best_ask is not None and self.best_bid is not None
        return float((self.best_ask - self.best_bid) / mid * Decimal(10_000))

    @property
    def microprice(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        bid_qty = self.bids[0].quantity if self.bids else ZERO
        ask_qty = self.asks[0].quantity if self.asks else ZERO
        total = bid_qty + ask_qty
        if total <= 0:
            return self.mid
        return (self.best_bid * ask_qty + self.best_ask * bid_qty) / total

    def depth_notional(self, side: Side, levels: int = 5) -> Decimal:
        book = self.bids if side == Side.BUY else self.asks
        return sum((level.notional for level in book[:levels]), ZERO)

    def depth_quantity(self, side: Side, levels: int = 5) -> Decimal:
        book = self.bids if side == Side.BUY else self.asks
        return sum((level.quantity for level in book[:levels]), ZERO)

    def top5_depth_tmn(self) -> float:
        bid = float(self.depth_notional(Side.BUY, 5))
        ask = float(self.depth_notional(Side.SELL, 5))
        return min(bid, ask)

    def imbalance(self, levels: int = 5) -> float:
        bid = self.depth_notional(Side.BUY, levels)
        ask = self.depth_notional(Side.SELL, levels)
        total = bid + ask
        if total <= 0:
            return 0.0
        return float((bid - ask) / total)

    def cumulative_depth(self, side: Side, bps: float) -> Decimal:
        mid = self.mid
        if mid is None:
            return ZERO
        if side == Side.BUY:
            threshold = mid * (Decimal(1) - Decimal(str(bps)) / Decimal(10_000))
            return sum((lvl.notional for lvl in self.bids if lvl.price >= threshold), ZERO)
        threshold = mid * (Decimal(1) + Decimal(str(bps)) / Decimal(10_000))
        return sum((lvl.notional for lvl in self.asks if lvl.price <= threshold), ZERO)

    def age_ms(self, now: datetime | None = None) -> float:
        current = now or utcnow()
        return max(0.0, (current - self.updated_at).total_seconds() * 1000)

    def is_healthy(self, max_age_ms: int) -> bool:
        return (
            bool(self.bids)
            and bool(self.asks)
            and self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > 0
            and self.best_ask > 0
            and self.best_bid <= self.best_ask
            and self.age_ms() <= max_age_ms
        )


@dataclass(slots=True)
class TradeTick:
    symbol: str
    price: Decimal
    quantity: Decimal
    is_buy_order: bool
    timestamp: datetime
    trade_id: str | None = None

    @property
    def taker_side(self) -> Side:
        # Wallex reports the maker side in isBuyOrder.
        return Side.SELL if self.is_buy_order else Side.BUY

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity


@dataclass(slots=True)
class Candle:
    symbol: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_row(self) -> tuple[float, ...]:
        return (self.open, self.high, self.low, self.close, self.volume)


@dataclass(slots=True)
class Candidate:
    symbol: str
    side: Side
    signal: str
    score: float
    expected_edge_bps: float
    spread_bps: float
    top5_depth_tmn: float
    features: dict[str, float | int | str | None]
    regime: str
    rationale: str
    expected_move_bps: float = 0.0
    recommended_stop_bps: float = 100.0
    recommended_take_profit_bps: float = 180.0
    recommended_horizon_sec: int = 1200
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "signal": self.signal,
            "score": round(self.score, 6),
            "expected_edge_bps": round(self.expected_edge_bps, 3),
            "expected_move_bps": round(self.expected_move_bps, 3),
            "spread_bps": round(self.spread_bps, 3),
            "top5_depth_tmn": round(self.top5_depth_tmn, 2),
            "regime": self.regime,
            "rationale": self.rationale,
            "recommended_stop_bps": self.recommended_stop_bps,
            "recommended_take_profit_bps": self.recommended_take_profit_bps,
            "recommended_horizon_sec": self.recommended_horizon_sec,
            "features": self.features,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(slots=True)
class Position:
    symbol: str
    quantity: Decimal
    avg_entry: Decimal
    stop_price: Decimal
    take_profit_price: Decimal
    opened_at: datetime
    horizon_sec: int
    decision_id: str
    signal: str = "unknown"
    confidence: float = 0.0
    rationale: str = ""
    regime: str = ""
    high_water: Decimal = ZERO
    low_water: Decimal = ZERO
    entry_fee_tmn: Decimal = ZERO
    realized_pnl_tmn: Decimal = ZERO
    last_review_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def update_watermarks(self, price: Decimal, low: Decimal | None = None) -> None:
        if self.high_water == ZERO or price > self.high_water:
            self.high_water = price
        observed_low = price if low is None else low
        if self.low_water == ZERO or observed_low < self.low_water:
            self.low_water = observed_low

    def unrealized(self, mark: Decimal | None) -> Decimal:
        if mark is None:
            return ZERO
        return (mark - self.avg_entry) * self.quantity

    def age_sec(self, now: datetime | None = None) -> float:
        return max(0.0, ((now or utcnow()) - self.opened_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": str(self.quantity),
            "avg_entry": str(self.avg_entry),
            "stop_price": str(self.stop_price),
            "take_profit_price": str(self.take_profit_price),
            "opened_at": self.opened_at.isoformat(),
            "horizon_sec": self.horizon_sec,
            "decision_id": self.decision_id,
            "signal": self.signal,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "regime": self.regime,
            "high_water": str(self.high_water),
            "low_water": str(self.low_water),
            "entry_fee_tmn": str(self.entry_fee_tmn),
            "realized_pnl_tmn": str(self.realized_pnl_tmn),
            "last_review_at": self.last_review_at.isoformat() if self.last_review_at else None,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Position:
        return cls(
            symbol=str(data["symbol"]),
            quantity=Decimal(str(data["quantity"])),
            avg_entry=Decimal(str(data["avg_entry"])),
            stop_price=Decimal(str(data["stop_price"])),
            take_profit_price=Decimal(str(data["take_profit_price"])),
            opened_at=datetime.fromisoformat(str(data["opened_at"])),
            horizon_sec=int(data.get("horizon_sec", 1200)),
            decision_id=str(data.get("decision_id", "")),
            signal=str(data.get("signal", "unknown")),
            confidence=float(data.get("confidence", 0.0)),
            rationale=str(data.get("rationale", "")),
            regime=str(data.get("regime", "")),
            high_water=Decimal(str(data.get("high_water", "0"))),
            low_water=Decimal(str(data.get("low_water", "0"))),
            entry_fee_tmn=Decimal(str(data.get("entry_fee_tmn", "0"))),
            realized_pnl_tmn=Decimal(str(data.get("realized_pnl_tmn", "0"))),
            last_review_at=(
                datetime.fromisoformat(str(data["last_review_at"]))
                if data.get("last_review_at")
                else None
            ),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass(slots=True)
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    ts: datetime = field(default_factory=utcnow)
    liquidity: str = "taker"
    decision_id: str | None = None

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "fee": str(self.fee),
            "fee_asset": self.fee_asset,
            "ts": self.ts.isoformat(),
            "liquidity": self.liquidity,
            "decision_id": self.decision_id,
        }


@dataclass(slots=True)
class Order:
    client_order_id: str
    exchange_order_id: str | None
    symbol: str
    side: Side
    order_type: OrderType
    price: Decimal
    quantity: Decimal
    status: OrderStatus
    filled_quantity: Decimal = ZERO
    avg_fill_price: Decimal = ZERO
    fee_tmn: Decimal = ZERO
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    decision_id: str | None = None
    reason: str = ""
    reprices: int = 0
    raw_request: dict[str, Any] = field(default_factory=dict)
    raw_response: dict[str, Any] = field(default_factory=dict)
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, self.quantity - self.filled_quantity)

    @property
    def age_sec(self) -> float:
        return max(0.0, (utcnow() - self.created_at).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "status": self.status.value,
            "filled_quantity": str(self.filled_quantity),
            "avg_fill_price": str(self.avg_fill_price),
            "fee_tmn": str(self.fee_tmn),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "decision_id": self.decision_id,
            "reason": self.reason,
            "reprices": self.reprices,
            "fills": [f.to_dict() for f in self.fills],
        }


@dataclass(slots=True)
class PortfolioSnapshot:
    ts: datetime
    cash_tmn: Decimal
    positions_value_tmn: Decimal
    equity_tmn: Decimal
    realized_pnl_tmn: Decimal
    unrealized_pnl_tmn: Decimal
    fees_tmn: Decimal
    gross_exposure_pct: float
    cash_buffer_pct: float
    open_positions: int
    portfolio_heat_pct: float
    peak_equity_tmn: Decimal
    drawdown_pct: float
    daily_pnl_pct: float
    net_return_pct: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "cash_tmn": str(self.cash_tmn),
            "positions_value_tmn": str(self.positions_value_tmn),
            "equity_tmn": str(self.equity_tmn),
            "realized_pnl_tmn": str(self.realized_pnl_tmn),
            "unrealized_pnl_tmn": str(self.unrealized_pnl_tmn),
            "fees_tmn": str(self.fees_tmn),
            "gross_exposure_pct": round(self.gross_exposure_pct, 6),
            "cash_buffer_pct": round(self.cash_buffer_pct, 6),
            "open_positions": self.open_positions,
            "portfolio_heat_pct": round(self.portfolio_heat_pct, 6),
            "peak_equity_tmn": str(self.peak_equity_tmn),
            "drawdown_pct": round(self.drawdown_pct, 6),
            "daily_pnl_pct": round(self.daily_pnl_pct, 6),
            "net_return_pct": round(self.net_return_pct, 6),
        }
