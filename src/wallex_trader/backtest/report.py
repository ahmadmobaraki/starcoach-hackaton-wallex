from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from wallex_trader.config import Settings

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
STATIC_DIR = Path(__file__).resolve().parents[1] / "dashboard" / "static"


def write_backtest_artifacts(
    *,
    settings: Settings,
    run_id: str,
    metrics: dict[str, Any],
    equity: pd.DataFrame,
    trades: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    data_manifest: dict[str, Any],
    git_sha: str,
    period: str,
) -> dict[str, str]:
    artifact_dir = Path(settings.artifacts_dir) / "backtests" / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)
    equity_path = artifact_dir / "equity.csv"
    trades_path = artifact_dir / "trades.csv"
    decisions_path = artifact_dir / "decisions.jsonl"
    metrics_path = artifact_dir / "metrics.json"
    config_path = artifact_dir / "config.json"
    manifest_path = artifact_dir / "data_manifest.json"
    sha_path = artifact_dir / "git_sha.txt"
    report_path = artifact_dir / "report.html"

    equity.to_csv(equity_path, index=False)
    pd.DataFrame(trades).to_csv(trades_path, index=False)
    with decisions_path.open("w", encoding="utf-8") as handle:
        for decision in decisions:
            handle.write(json.dumps(decision, ensure_ascii=False, default=str) + "\n")
    metrics_path.write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    config_path.write_text(
        json.dumps(settings.safe_dump(redact=True), indent=2, default=str), encoding="utf-8"
    )
    manifest_path.write_text(json.dumps(data_manifest, indent=2, default=str), encoding="utf-8")
    sha_path.write_text(git_sha + "\n", encoding="utf-8")

    chart_path = STATIC_DIR / "chart.umd.min.js"
    if chart_path.exists():
        shutil.copyfile(chart_path, artifact_dir / "chart.umd.min.js")

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("report.html")
    chart_frame = equity
    if len(equity) > 2000:
        stride = max(1, len(equity) // 2000)
        chart_frame = equity.iloc[::stride].copy()
        if not chart_frame.empty and equity.index[-1] != chart_frame.index[-1]:
            chart_frame = pd.concat([chart_frame, equity.iloc[[-1]]])
    labels = [str(value)[:19] for value in chart_frame["ts"].tolist()] if not chart_frame.empty else []
    values = [round(float(value), 2) for value in chart_frame["equity"].tolist()] if not chart_frame.empty else []
    drawdowns = [round(float(value), 4) for value in chart_frame["drawdown_pct"].tolist()] if not chart_frame.empty else []
    latest_trades = list(reversed(trades[-200:]))
    decision_counts: dict[str, int] = {}
    for decision in decisions:
        action = str(decision.get("action", "UNKNOWN"))
        decision_counts[action] = decision_counts.get(action, 0) + 1
    html = template.render(
        run_id=run_id,
        period=period,
        git_sha=git_sha,
        metrics=metrics,
        trades=latest_trades,
        fees_maker=settings.backtest_maker_fee,
        fees_taker=settings.backtest_taker_fee,
        config_json=json.dumps(settings.safe_dump(redact=True), indent=2, default=str),
        decision_summary_json=json.dumps(decision_counts, indent=2),
        equity_labels=json.dumps(labels),
        equity_values=json.dumps(values),
        drawdown_values=json.dumps(drawdowns),
    )
    report_path.write_text(html, encoding="utf-8")
    summary = {
        "run_id": run_id,
        "artifact_dir": str(artifact_dir),
        "report": str(report_path),
        "metrics": str(metrics_path),
        "equity": str(equity_path),
        "trades": str(trades_path),
        "decisions": str(decisions_path),
        "config": str(config_path),
        "data_manifest": str(manifest_path),
        "git_sha": git_sha,
        "created_at": datetime.now(UTC).isoformat(),
    }
    (artifact_dir / "artifact_index.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    return summary
