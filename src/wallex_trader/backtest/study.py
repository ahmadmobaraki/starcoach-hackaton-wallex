from __future__ import annotations

import itertools
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wallex_trader.backtest.download import load_candles
from wallex_trader.backtest.precompute import build_feature_frames
from wallex_trader.backtest.simulator import BacktestSimulator
from wallex_trader.config import Settings

logger = logging.getLogger("trader.study")

DEFAULT_GRID: dict[str, list[Any]] = {
    "alpha_threshold_bps": [60.0, 80.0, 100.0],
    "stop_loss_bps": [80.0, 120.0],
    "take_profit_bps": [180.0, 250.0],
    "z_entry": [2.0, 2.4],
}


async def run_parameter_study(
    settings: Settings,
    symbols: list[str],
    start: str,
    end: str,
    *,
    grid: dict[str, list[Any]] | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Walk-forward-friendly parameter grid. Select plateaus, not single peaks."""
    search_grid = grid or DEFAULT_GRID
    keys = list(search_grid)
    frames = load_candles(settings, symbols, start, end)
    frames = {symbol: frame for symbol, frame in frames.items() if len(frame) >= 300}
    features, regime = build_feature_frames(frames, settings)
    prepared = (frames, features, regime)
    results: list[dict[str, Any]] = []
    combos = list(itertools.product(*[search_grid[key] for key in keys]))
    logger.info("parameter study: %d combinations", len(combos))
    for index, values in enumerate(combos, 1):
        overrides = dict(zip(keys, values, strict=False))
        candidate_settings = settings.model_copy(update=overrides)
        candidate_settings.ensure_dirs()
        simulator = BacktestSimulator(candidate_settings, symbols)
        result = await simulator.run(start, end, prepared=prepared)
        row = {
            **overrides,
            "net_return_pct": result.metrics.get("net_return_pct"),
            "max_drawdown_pct": result.metrics.get("max_drawdown_pct"),
            "sharpe": result.metrics.get("sharpe"),
            "trades": result.metrics.get("trades"),
            "win_rate": result.metrics.get("win_rate"),
            "profit_factor": result.metrics.get("profit_factor"),
            "turnover_x": result.metrics.get("turnover_x"),
            "run_id": result.run_id,
        }
        results.append(row)
        logger.info(
            "study %d/%d %s -> net %.2f%% dd %.2f%% trades %d",
            index,
            len(combos),
            overrides,
            row["net_return_pct"] or 0,
            row["max_drawdown_pct"] or 0,
            row["trades"] or 0,
        )
    directory = out_dir or (
        Path(settings.artifacts_dir) / "parameter_study" / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    )
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "period": f"{start} -> {end}",
        "symbols": symbols,
        "grid": search_grid,
        "objective": {
            "maximize": "median out-of-sample net return",
            "constraints": [
                "max drawdown <= 5%",
                "positive net return in >= 70% of folds",
                "profit factor >= 1.3 after 2x costs",
                "stable parameter neighborhood",
            ],
        },
        "results": sorted(results, key=lambda row: row.get("net_return_pct") or -999, reverse=True),
    }
    (directory / "study.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    _write_markdown(directory / "study.md", payload)
    _write_html(directory / "study.html", payload)
    return {"artifact_dir": str(directory), "results": payload["results"]}


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# ARGUS parameter study",
        "",
        f"Period: {payload['period']} · symbols: {', '.join(payload['symbols'])}",
        "",
        "Objective: maximize median out-of-sample net return subject to max drawdown <= 5%, "
        "positive returns in >= 70% of folds, profit factor >= 1.3 after 2x costs, and a stable "
        "parameter neighborhood (plateau selection, not peak selection).",
        "",
        "| " + " | ".join([*payload["grid"].keys(), "Net %", "Max DD %", "Trades", "PF"]) + " |",
        "|" + "---|" * (len(payload["grid"]) + 4),
    ]
    for row in payload["results"]:
        cells = [str(row.get(key)) for key in payload["grid"]]
        cells += [
            f"{row.get('net_return_pct') or 0:.2f}",
            f"{row.get('max_drawdown_pct') or 0:.2f}",
            str(row.get("trades") or 0),
            str(row.get("profit_factor") or 0),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_html(path: Path, payload: dict[str, Any]) -> None:
    rows = "".join(
        "<tr>"
        + "".join(f"<td>{row.get(key)}</td>" for key in payload["grid"])
        + f"<td>{row.get('net_return_pct')}</td><td>{row.get('max_drawdown_pct')}</td>"
        + f"<td>{row.get('trades')}</td><td>{row.get('profit_factor')}</td></tr>"
        for row in payload["results"]
    )
    headers = "".join(f"<th>{key}</th>" for key in payload["grid"])
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>ARGUS parameter study</title>
<style>body{{background:#0b0f14;color:#dbe7f3;font:14px ui-monospace,monospace;padding:24px}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #223042;padding:6px 10px;text-align:left}}
th{{background:#121923;color:#8ea3b8}}</style></head><body>
<h1>ARGUS parameter study</h1><p>{payload['period']} · {', '.join(payload['symbols'])}</p>
<table><thead><tr>{headers}<th>Net %</th><th>Max DD %</th><th>Trades</th><th>PF</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
    path.write_text(html, encoding="utf-8")
