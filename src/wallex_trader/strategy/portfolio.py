from __future__ import annotations

from collections import deque
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.models import ZERO, Fill, OrderBook, PortfolioSnapshot, Position, Side

CORRELATED = {"BTCTMN", "ETHTMN", "SOLTMN"}


class PortfolioError(RuntimeError):
    pass


class Portfolio:
    def __init__(self, settings: Settings, initial_cash_tmn: Decimal | float | None = None) -> None:
        cash = (
            Decimal(str(initial_cash_tmn))
            if initial_cash_tmn is not None
            else Decimal(str(settings.initial_capital_tmn))
        )
        self.settings = settings
        self.initial_cash_tmn = cash
        self.cash_tmn = cash
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, Decimal] = {}
        self.realized_pnl_tmn = ZERO
        self.fees_tmn = ZERO
        self.peak_equity_tmn = cash
        self.day_start_equity_tmn = cash
        self.day_date: date = datetime.now(UTC).date()
        self.consecutive_losses = 0
        self.cooldown_until: datetime | None = None
        self.reserved_cash_tmn = ZERO
        self.total_volume_tmn = ZERO
        self.closed_trades: deque[dict[str, Any]] = deque(maxlen=500)
        self.wins = 0
        self.losses = 0
        self.last_snapshot: PortfolioSnapshot | None = None

    # ------------------------------------------------------------------
    def marks_for(self, books: dict[str, OrderBook]) -> None:
        for symbol, position in self.positions.items():
            book = books.get(symbol)
            if book and book.best_bid is not None:
                self.marks[symbol] = book.best_bid
                position.update_watermarks(book.best_bid)

    def mark(self, symbol: str, price: Decimal) -> None:
        self.marks[symbol] = price
        position = self.positions.get(symbol)
        if position:
            position.update_watermarks(price)

    def mark_price(self, symbol: str) -> Decimal | None:
        return self.marks.get(symbol)

    # ------------------------------------------------------------------
    def positions_value_tmn(self) -> Decimal:
        total = ZERO
        for symbol, position in self.positions.items():
            mark = self.marks.get(symbol, position.avg_entry)
            total += mark * position.quantity
        return total

    def unrealized_pnl_tmn(self) -> Decimal:
        return sum(
            (
                (self.marks.get(symbol, position.avg_entry) - position.avg_entry)
                * position.quantity
                for symbol, position in self.positions.items()
            ),
            ZERO,
        )

    def equity_tmn(self) -> Decimal:
        # Reserved cash is still part of cash; it is only unavailable for new
        # orders. Do not add it again or partial fills double-count equity.
        return self.cash_tmn + self.positions_value_tmn()

    def gross_exposure_pct(self) -> float:
        equity = self.equity_tmn()
        if equity <= 0:
            return 1.0
        return float(self.positions_value_tmn() / equity)

    def cash_buffer_pct(self) -> float:
        equity = self.equity_tmn()
        if equity <= 0:
            return 0.0
        return float((self.cash_tmn - self.reserved_cash_tmn) / equity)

    def correlated_exposure_pct(self) -> float:
        equity = self.equity_tmn()
        if equity <= 0:
            return 0.0
        value = sum(
            (
                self.marks.get(symbol, position.avg_entry) * position.quantity
                for symbol, position in self.positions.items()
                if symbol in CORRELATED
            ),
            ZERO,
        )
        return float(value / equity)

    def portfolio_heat_pct(self) -> float:
        equity = self.equity_tmn()
        if equity <= 0:
            return 1.0
        heat = ZERO
        for symbol, position in self.positions.items():
            mark = self.marks.get(symbol, position.avg_entry)
            risk_per_unit = max(ZERO, mark - position.stop_price)
            heat += risk_per_unit * position.quantity
        return float(heat / equity)

    def drawdown_pct(self) -> float:
        equity = self.equity_tmn()
        if self.peak_equity_tmn <= 0:
            return 0.0
        return max(0.0, float((self.peak_equity_tmn - equity) / self.peak_equity_tmn))

    def daily_pnl_pct(self) -> float:
        if self.day_start_equity_tmn <= 0:
            return 0.0
        return float((self.equity_tmn() - self.day_start_equity_tmn) / self.day_start_equity_tmn)

    def net_return_pct(self) -> float:
        if self.initial_cash_tmn <= 0:
            return 0.0
        return float((self.equity_tmn() - self.initial_cash_tmn) / self.initial_cash_tmn)

    def snapshot(self, now: datetime | None = None) -> PortfolioSnapshot:
        current = now or datetime.now(UTC)
        self._daily_rollover(current)
        equity = self.equity_tmn()
        if equity > self.peak_equity_tmn:
            self.peak_equity_tmn = equity
        snapshot = PortfolioSnapshot(
            ts=current,
            cash_tmn=self.cash_tmn,
            positions_value_tmn=self.positions_value_tmn(),
            equity_tmn=equity,
            realized_pnl_tmn=self.realized_pnl_tmn,
            unrealized_pnl_tmn=self.unrealized_pnl_tmn(),
            fees_tmn=self.fees_tmn,
            gross_exposure_pct=self.gross_exposure_pct(),
            cash_buffer_pct=self.cash_buffer_pct(),
            open_positions=len([p for p in self.positions.values() if p.quantity > 0]),
            portfolio_heat_pct=self.portfolio_heat_pct(),
            peak_equity_tmn=self.peak_equity_tmn,
            drawdown_pct=self.drawdown_pct(),
            daily_pnl_pct=self.daily_pnl_pct(),
            net_return_pct=self.net_return_pct(),
        )
        self.last_snapshot = snapshot
        return snapshot

    # ------------------------------------------------------------------
    def reserve_cash(self, amount: Decimal) -> None:
        self.reserved_cash_tmn += max(ZERO, amount)

    def release_cash(self, amount: Decimal) -> None:
        self.reserved_cash_tmn = max(ZERO, self.reserved_cash_tmn - max(ZERO, amount))

    def can_afford(self, notional: Decimal, fee: Decimal) -> bool:
        return self.cash_tmn - self.reserved_cash_tmn >= notional + fee

    # ------------------------------------------------------------------
    def apply_fill(
        self,
        fill: Fill,
        *,
        entry_meta: dict[str, Any] | None = None,
        exit_reason: str = "signal",
    ) -> dict[str, Any] | None:
        """Apply a fill and return a closed-trade record when a position closes."""
        self._daily_rollover(fill.ts)
        if fill.side == Side.BUY:
            total_cost = fill.notional + fill.fee
            if total_cost > self.cash_tmn + Decimal("1"):
                raise PortfolioError(
                    f"insufficient cash: need {total_cost}, have {self.cash_tmn}"
                )
            self.cash_tmn -= total_cost
            self.fees_tmn += fill.fee
            self.total_volume_tmn += fill.notional
            existing = self.positions.get(fill.symbol)
            if existing is None:
                meta = entry_meta or {}
                stop_bps = float(meta.get("stop_bps", self.settings.stop_loss_bps))
                take_bps = float(meta.get("take_profit_bps", self.settings.take_profit_bps))
                position = Position(
                    symbol=fill.symbol,
                    quantity=fill.quantity,
                    avg_entry=fill.price,
                    stop_price=fill.price * (Decimal(1) - Decimal(str(stop_bps)) / Decimal(10_000)),
                    take_profit_price=fill.price
                    * (Decimal(1) + Decimal(str(take_bps)) / Decimal(10_000)),
                    opened_at=fill.ts,
                    horizon_sec=int(meta.get("horizon_sec", self.settings.time_stop_sec)),
                    decision_id=str(meta.get("decision_id", fill.decision_id or "")),
                    signal=str(meta.get("signal", "unknown")),
                    confidence=float(meta.get("confidence", 0.0)),
                    rationale=str(meta.get("rationale", "")),
                    regime=str(meta.get("regime", "")),
                    high_water=fill.price,
                    low_water=fill.price,
                    entry_fee_tmn=fill.fee,
                    metadata={
                        **meta.get("features", {}),
                        "agent": meta.get("agent", {}),
                        "entry_price": str(fill.price),
                        "entry_ts": fill.ts.isoformat(),
                    },
                )
                self.positions[fill.symbol] = position
            else:
                total_qty = existing.quantity + fill.quantity
                existing.avg_entry = (
                    existing.avg_entry * existing.quantity + fill.price * fill.quantity
                ) / total_qty
                existing.quantity = total_qty
                existing.entry_fee_tmn += fill.fee
                if entry_meta:
                    stop_bps = float(entry_meta.get("stop_bps", self.settings.stop_loss_bps))
                    existing.stop_price = max(
                        existing.stop_price,
                        fill.price * (Decimal(1) - Decimal(str(stop_bps)) / Decimal(10_000)),
                    )
            self.mark(fill.symbol, fill.price)
            self._update_peak()
            return None

        # SELL
        sell_position = self.positions.get(fill.symbol)
        if sell_position is None:
            raise PortfolioError(f"no position to sell for {fill.symbol}")
        qty = min(fill.quantity, sell_position.quantity)
        if qty <= 0:
            raise PortfolioError("sell fill has zero quantity")
        proceeds = fill.price * qty - fill.fee
        self.cash_tmn += proceeds
        self.fees_tmn += fill.fee
        self.total_volume_tmn += fill.price * qty
        entry_fee_share = (
            sell_position.entry_fee_tmn * (qty / sell_position.quantity)
            if sell_position.quantity > 0
            else ZERO
        )
        gross_pnl = (fill.price - sell_position.avg_entry) * qty
        net_pnl = gross_pnl - entry_fee_share - fill.fee
        self.realized_pnl_tmn += net_pnl
        sell_position.quantity -= qty
        sell_position.entry_fee_tmn -= entry_fee_share
        sell_position.realized_pnl_tmn += net_pnl
        self.mark(fill.symbol, fill.price)
        trade: dict[str, Any] | None = None
        if sell_position.quantity <= Decimal("1e-12"):
            duration = max(0.0, (fill.ts - sell_position.opened_at).total_seconds())
            return_bps = (
                float((fill.price - sell_position.avg_entry) / sell_position.avg_entry * 10_000)
                if sell_position.avg_entry > 0
                else 0.0
            )
            trade = {
                "symbol": fill.symbol,
                "entry_ts": sell_position.opened_at.isoformat(),
                "exit_ts": fill.ts.isoformat(),
                "duration_sec": duration,
                "quantity": str(qty),
                "entry_price": str(sell_position.avg_entry),
                "exit_price": str(fill.price),
                "entry_notional_tmn": str(sell_position.avg_entry * qty),
                "exit_notional_tmn": str(fill.price * qty),
                "gross_pnl_tmn": str(gross_pnl),
                "pnl_tmn": str(net_pnl),
                "fees_tmn": str(entry_fee_share + fill.fee),
                "return_bps": round(return_bps, 2),
                "win": net_pnl > 0,
                "signal": sell_position.signal,
                "confidence": sell_position.confidence,
                "rationale": sell_position.rationale,
                "regime": sell_position.regime,
                "decision_id": sell_position.decision_id,
                "exit_decision_id": fill.decision_id,
                "exit_reason": exit_reason,
                "features": {
                    key: value
                    for key, value in sell_position.metadata.items()
                    if key not in ("agent",)
                },
                "agent": sell_position.metadata.get("agent", {}),
            }
            self.closed_trades.append(trade)
            if net_pnl > 0:
                self.wins += 1
                self.consecutive_losses = 0
            else:
                self.losses += 1
                self.consecutive_losses += 1
                if self.consecutive_losses >= self.settings.max_consecutive_losses:
                    from datetime import timedelta

                    self.cooldown_until = fill.ts + timedelta(
                        seconds=self.settings.losing_streak_cooldown_sec
                    )
            del self.positions[fill.symbol]
            self.marks.pop(fill.symbol, None)
        self._update_peak()
        return trade

    def _update_peak(self) -> None:
        equity = self.equity_tmn()
        if equity > self.peak_equity_tmn:
            self.peak_equity_tmn = equity

    def _daily_rollover(self, now: datetime) -> None:
        if now.date() == self.day_date:
            return
        self.day_date = now.date()
        self.day_start_equity_tmn = self.equity_tmn()
        self.consecutive_losses = 0
        self.cooldown_until = None

    def cooldown_active(self, now: datetime | None = None) -> bool:
        current = now or datetime.now(UTC)
        return self.cooldown_until is not None and current < self.cooldown_until

    def current_equity_tmn(self) -> Decimal:
        return self.equity_tmn()

    # ------------------------------------------------------------------
    def restore_positions(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            try:
                position = Position.from_dict(row)
            except (KeyError, ValueError, TypeError):
                continue
            if position.quantity > 0:
                self.positions[position.symbol] = position
                self.marks[position.symbol] = position.avg_entry

    def recent_trades(self, limit: int = 10) -> list[dict[str, Any]]:
        return list(self.closed_trades)[-limit:]

    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0

    def open_symbols(self) -> list[str]:
        return [symbol for symbol, position in self.positions.items() if position.quantity > 0]

    def state(self) -> dict[str, Any]:
        return {
            "cash_tmn": str(self.cash_tmn),
            "reserved_cash_tmn": str(self.reserved_cash_tmn),
            "realized_pnl_tmn": str(self.realized_pnl_tmn),
            "fees_tmn": str(self.fees_tmn),
            "peak_equity_tmn": str(self.peak_equity_tmn),
            "day_start_equity_tmn": str(self.day_start_equity_tmn),
            "positions": {symbol: position.to_dict() for symbol, position in self.positions.items()},
            "closed_trades": list(self.closed_trades),
            "wins": self.wins,
            "losses": self.losses,
            "consecutive_losses": self.consecutive_losses,
        }
