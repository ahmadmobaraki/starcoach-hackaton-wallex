from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from wallex_trader.agent.schemas import AgentDecision
from wallex_trader.config import Settings
from wallex_trader.models import Fill, Side
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository
from wallex_trader.strategy.portfolio import Portfolio, PortfolioError

logger = logging.getLogger("trader.replay")


async def replay_run(settings: Settings, run_id: str | None = None) -> dict[str, Any]:
    db = create_database(settings)
    await db.init()
    repo = Repository(db, run_id or "replay")
    try:
        run = await repo.get_run(run_id) if run_id else await repo.latest_trading_run()
        if not run:
            raise RuntimeError("no run found to replay")
        active = str(run["run_id"])
        decisions = await repo.recent_decisions(active, limit=20_000)
        fills = await repo.list_fills(active, limit=100_000)
        equity = await repo.equity_series(active, limit=20_000)
        orders = await repo.list_orders(active, limit=20_000)
        checks: list[dict[str, Any]] = []

        def add(name: str, ok: bool, detail: Any = None) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        decision_ids = [row["decision_id"] for row in decisions]
        add(
            "decision_ids_unique",
            len(decision_ids) == len(set(decision_ids)),
            {"decisions": len(decisions), "unique": len(set(decision_ids))},
        )
        add(
            "orders_have_decision",
            all(order.get("decision_id") for order in orders),
            {"orders": len(orders), "missing": sum(1 for order in orders if not order.get("decision_id"))},
        )
        add(
            "fills_have_decision",
            all(fill.get("decision_id") for fill in fills),
            {"fills": len(fills), "missing": sum(1 for fill in fills if not fill.get("decision_id"))},
        )

        hash_failures = []
        for decision in decisions:
            agent = decision.get("agent") or {}
            if not agent or not decision.get("response_hash"):
                continue
            try:
                rebuilt = AgentDecision(
                    action=agent.get("action", decision.get("action", "HOLD")),
                    confidence=float(agent.get("confidence", decision.get("confidence", 0.0))),
                    size_fraction=float(agent.get("size_fraction", 0.0)),
                    order_type=agent.get("order_type", "LIMIT"),
                    limit_offset_bps=float(agent.get("limit_offset_bps", 0.0)),
                    take_profit_bps=float(agent.get("take_profit_bps", settings.take_profit_bps)),
                    stop_loss_bps=float(agent.get("stop_loss_bps", settings.stop_loss_bps)),
                    horizon_sec=int(agent.get("horizon_sec", settings.time_stop_sec)),
                    regime=str(agent.get("regime", "unknown")),
                    rationale=str(agent.get("rationale", "")),
                    risk_flags=list(agent.get("risk_flags", [])),
                )
                expected = rebuilt.response_hash()
                if expected != decision.get("response_hash"):
                    hash_failures.append(
                        {
                            "decision_id": decision.get("decision_id"),
                            "expected": expected,
                            "stored": decision.get("response_hash"),
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                hash_failures.append(
                    {"decision_id": decision.get("decision_id"), "error": str(exc)[:200]}
                )
        add("agent_response_hashes", not hash_failures, {"failures": hash_failures[:20]})

        rebuilt_portfolio = Portfolio(settings, Decimal(str(settings.initial_capital_tmn)))
        decision_map = {row["decision_id"]: row for row in decisions}
        replay_errors: list[str] = []
        for fill_row in sorted(fills, key=lambda row: str(row.get("ts"))):
            try:
                fill = Fill(
                    fill_id=str(fill_row["fill_id"]),
                    order_id=str(fill_row["order_id"]),
                    symbol=str(fill_row["symbol"]),
                    side=Side(str(fill_row["side"])),
                    price=Decimal(str(fill_row["price"])),
                    quantity=Decimal(str(fill_row["quantity"])),
                    fee=Decimal(str(fill_row["fee"])),
                    fee_asset=str(fill_row.get("fee_asset", "TMN")),
                    ts=datetime.fromisoformat(str(fill_row["ts"])),
                    liquidity=str(fill_row.get("liquidity", "taker")),
                    decision_id=fill_row.get("decision_id"),
                )
                decision_row: dict[str, Any] | None = decision_map.get(
                    str(fill_row.get("decision_id") or "")
                )
                entry_meta: dict[str, Any] | None = None
                exit_reason = "replay_exit"
                if fill.side == Side.BUY and decision_row:
                    agent = decision_row.get("agent") or {}
                    entry_meta = {
                        "decision_id": decision_row.get("decision_id"),
                        "signal": decision_row.get("signal", "replay"),
                        "confidence": float(
                            agent.get("confidence", decision_row.get("confidence", 0.0))
                        ),
                        "rationale": str(decision_row.get("rationale", "")),
                        "regime": str(agent.get("regime", "unknown")),
                        "stop_bps": float(agent.get("stop_loss_bps", settings.stop_loss_bps)),
                        "take_profit_bps": float(
                            agent.get("take_profit_bps", settings.take_profit_bps)
                        ),
                        "horizon_sec": int(agent.get("horizon_sec", settings.time_stop_sec)),
                        "features": decision_row.get("features") or {},
                    }
                elif decision_row:
                    execution = decision_row.get("execution") or {}
                    exit_reason = str(
                        execution.get("trigger")
                        or (decision_row.get("risk") or {}).get("reason")
                        or "replay_exit"
                    )
                rebuilt_portfolio.apply_fill(
                    fill, entry_meta=entry_meta, exit_reason=exit_reason
                )
            except (PortfolioError, ValueError, KeyError) as exc:
                replay_errors.append(f"{fill_row.get('fill_id')}: {exc}")
        add("fills_replayed", not replay_errors, {"errors": replay_errors[:20]})

        latest = equity[-1] if equity else None
        reconstructed = rebuilt_portfolio.state()
        if latest:
            expected_realized = Decimal(str(latest.get("realized_pnl_tmn", "0")))
            expected_fees = Decimal(str(latest.get("fees_tmn", "0")))
            realized_ok = abs(rebuilt_portfolio.realized_pnl_tmn - expected_realized) <= Decimal("1")
            fees_ok = abs(rebuilt_portfolio.fees_tmn - expected_fees) <= Decimal("1")
            add(
                "realized_pnl_reconciles",
                realized_ok,
                {
                    "reconstructed": str(rebuilt_portfolio.realized_pnl_tmn),
                    "recorded": str(expected_realized),
                },
            )
            add(
                "fees_reconcile",
                fees_ok,
                {
                    "reconstructed": str(rebuilt_portfolio.fees_tmn),
                    "recorded": str(expected_fees),
                },
            )
        else:
            add("equity_snapshot_present", False, "no equity snapshots")

        ok = all(check["ok"] for check in checks)
        artifact_dir = Path(settings.artifacts_dir) / "replay" / active
        artifact_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "generated_at": datetime.now(UTC).isoformat(),
            "run_id": active,
            "mode": run.get("mode"),
            "git_sha": run.get("git_sha"),
            "config_hash": run.get("config_hash"),
            "ok": ok,
            "checks": checks,
            "decisions": len(decisions),
            "fills": len(fills),
            "reconstructed": reconstructed,
            "recorded_final_equity": latest,
            "limitations": [
                "Replay reconstructs portfolio accounting from persisted fills and decision metadata.",
                "It verifies response hashes and decision/order/fill linkage, not the original WebSocket bytes.",
                "AI decisions are not re-called unless a separate cached-decision backtest is run.",
            ],
        }
        (artifact_dir / "replay_report.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        lines = [
            "# ARGUS replay report",
            "",
            f"Run: `{active}` · mode: {run.get('mode')} · git: {run.get('git_sha')}",
            f"Result: {'PASS' if ok else 'FAIL'}",
            "",
            "| Check | Result | Detail |",
            "|---|---|---|",
        ]
        for check in checks:
            lines.append(
                f"| {check['name']} | {'PASS' if check['ok'] else 'FAIL'} | "
                f"`{json.dumps(check.get('detail'), default=str)[:300]}` |"
            )
        (artifact_dir / "replay_report.md").write_text("\n".join(lines), encoding="utf-8")
        report["artifact_dir"] = str(artifact_dir)
        return report
    finally:
        await db.close()
