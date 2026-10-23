from __future__ import annotations

import pandas as pd

from wallex_trader.backtest.metrics import compute_metrics


def test_metrics_math() -> None:
    equity = pd.DataFrame(
        {
            "ts": pd.date_range("2026-01-01", periods=5, freq="1min", tz="UTC"),
            "equity": [1000.0, 1010.0, 1005.0, 1020.0, 1030.0],
            "gross_exposure_pct": [0.0, 0.2, 0.2, 0.1, 0.0],
        }
    )
    trades = [
        {"symbol": "BTCTMN", "pnl_tmn": "30", "win": True, "duration_sec": 600,
         "entry_notional_tmn": "1000", "exit_notional_tmn": "1030"},
        {"symbol": "BTCTMN", "pnl_tmn": "-10", "win": False, "duration_sec": 300,
         "entry_notional_tmn": "1000", "exit_notional_tmn": "990"},
    ]
    metrics = compute_metrics(
        equity,
        trades,
        initial_capital=1000.0,
        fees_tmn=5.0,
        decisions=4,
        buy_decisions=2,
        fills=2,
        interval_minutes=1,
    )
    assert metrics["net_return_pct"] == 3.0
    assert metrics["trades"] == 2
    assert metrics["win_rate"] == 0.5
    assert metrics["profit_factor"] == 3.0
    assert metrics["max_drawdown_pct"] > 0
    assert metrics["fill_rate"] == 1.0
    assert metrics["per_symbol"]["BTCTMN"]["trades"] == 2
