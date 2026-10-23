from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

import numpy as np
import pandas as pd


def compute_metrics(
    equity: pd.DataFrame,
    trades: list[dict[str, Any]],
    *,
    initial_capital: float,
    fees_tmn: float = 0.0,
    decisions: int = 0,
    buy_decisions: int = 0,
    fills: int = 0,
    interval_minutes: float = 1.0,
) -> dict[str, Any]:
    if equity.empty:
        return {
            "net_return_pct": 0.0,
            "realized_pnl_tmn": 0.0,
            "fees_tmn": fees_tmn,
            "max_drawdown_pct": 0.0,
            "trades": 0,
        }
    equity_values = equity["equity"].astype(float).to_numpy()
    final_equity = float(equity_values[-1])
    net_return = (final_equity / initial_capital - 1) * 100 if initial_capital else 0.0
    returns = np.diff(equity_values) / np.maximum(equity_values[:-1], 1e-12)
    bars_per_day = max(1.0, 1440.0 / max(interval_minutes, 0.1))
    ann_factor = math.sqrt(365.0 * bars_per_day)
    sharpe = float(np.mean(returns) / np.std(returns) * ann_factor) if len(returns) > 1 and np.std(returns) > 0 else 0.0
    downside = returns[returns < 0]
    sortino = (
        float(np.mean(returns) / np.std(downside) * ann_factor)
        if len(downside) > 1 and np.std(downside) > 0
        else 0.0
    )
    peak = np.maximum.accumulate(equity_values)
    drawdowns = (peak - equity_values) / np.maximum(peak, 1e-12)
    max_dd = float(np.max(drawdowns) * 100) if len(drawdowns) else 0.0
    calmar = (net_return / max_dd) if max_dd > 0 else 0.0

    pnls = [float(trade.get("pnl_tmn", 0)) for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    win_rate = len(wins) / len(pnls) if pnls else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    expectancy = float(np.mean(pnls)) if pnls else 0.0
    holdings = [float(trade.get("duration_sec", 0)) for trade in trades]
    turnover = (
        sum(float(trade.get("entry_notional_tmn", 0)) + float(trade.get("exit_notional_tmn", 0)) for trade in trades)
        / initial_capital
        if initial_capital
        else 0.0
    )
    per_symbol: dict[str, dict[str, Any]] = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "wins": 0})
    for trade in trades:
        bucket = per_symbol[str(trade.get("symbol", "unknown"))]
        bucket["trades"] += 1
        bucket["pnl"] += float(trade.get("pnl_tmn", 0))
        if trade.get("win"):
            bucket["wins"] += 1
    for bucket in per_symbol.values():
        bucket["pnl"] = round(bucket["pnl"], 2)
        bucket["win_rate"] = round(bucket["wins"] / bucket["trades"], 4) if bucket["trades"] else 0.0
    exposure = (
        float(equity["gross_exposure_pct"].mean() * 100)
        if "gross_exposure_pct" in equity.columns
        else 0.0
    )
    best_trade = max(pnls) if pnls else 0.0
    worst_trade = min(pnls) if pnls else 0.0
    without_best = sum(pnls) - best_trade if pnls else 0.0
    return {
        "initial_capital_tmn": round(initial_capital, 2),
        "final_equity_tmn": round(final_equity, 2),
        "net_return_pct": round(net_return, 4),
        "realized_pnl_tmn": round(sum(pnls), 2),
        "fees_tmn": round(fees_tmn, 2),
        "max_drawdown_pct": round(max_dd, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "calmar": round(calmar, 3),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(profit_factor, 3) if math.isfinite(profit_factor) else 999.0,
        "expectancy_tmn": round(expectancy, 2),
        "avg_win_tmn": round(avg_win, 2),
        "avg_loss_tmn": round(avg_loss, 2),
        "best_trade_tmn": round(best_trade, 2),
        "worst_trade_tmn": round(worst_trade, 2),
        "pnl_without_best_trade_tmn": round(without_best, 2),
        "turnover_x": round(turnover, 3),
        "avg_holding_min": round(float(np.mean(holdings)) / 60, 2) if holdings else 0.0,
        "avg_exposure_pct": round(exposure, 2),
        "decisions": decisions,
        "buy_decisions": buy_decisions,
        "fills": fills,
        "fill_rate": round(fills / buy_decisions, 4) if buy_decisions else 0.0,
        "per_symbol": dict(per_symbol),
        "initial_capital_reference": initial_capital,
        "final_equity_reference": final_equity,
    }
