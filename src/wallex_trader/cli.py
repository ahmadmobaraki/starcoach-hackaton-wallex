from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wallex_trader.agent.ab_test import CachedDecisionAgent, run_ablation
from wallex_trader.agent.client import DecisionAgent, DeterministicAgent
from wallex_trader.agent.memory import AgentMemory
from wallex_trader.backtest.download import download_history
from wallex_trader.backtest.simulator import BacktestSimulator
from wallex_trader.backtest.study import DEFAULT_GRID, run_parameter_study
from wallex_trader.config import Settings, load_settings
from wallex_trader.engine import TradingEngine
from wallex_trader.exchange.rest import WallexRESTClient
from wallex_trader.logging import setup_logging
from wallex_trader.preflight import run_preflight
from wallex_trader.replay import replay_run
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository

logger = logging.getLogger("trader.cli")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="ARGUS — auditable AI trading agent for Wallex spot markets",
    )
    parser.add_argument("--config-dir", default=None, help="directory containing base.yaml and mode YAML")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run the trader in MODE from .env")
    run_parser.add_argument("--duration", type=float, default=None, help="stop after N seconds")
    run_parser.add_argument("--confirm-live", action="store_true")

    shadow_parser = sub.add_parser("shadow", help="run shadow mode (no real orders)")
    shadow_parser.add_argument("--duration", type=float, default=None)

    live_parser = sub.add_parser("live", help="run live mode (guarded)")
    live_parser.add_argument("--confirm-live", action="store_true")
    live_parser.add_argument("--duration", type=float, default=None)

    preflight_parser = sub.add_parser("preflight", help="verify REST, WS, MCP, DeepSeek and account")
    preflight_parser.add_argument("--no-ws", action="store_true")

    download_parser = sub.add_parser("download", help="download Wallex 1m candle history")
    download_parser.add_argument("--symbols", default=None)
    download_parser.add_argument("--start", required=True)
    download_parser.add_argument("--end", required=True)
    download_parser.add_argument("--resolution", default="1")
    download_parser.add_argument("--force", action="store_true")
    download_parser.add_argument("--out", default=None)

    backtest_parser = sub.add_parser("backtest", help="run the event-driven backtest")
    backtest_parser.add_argument("--symbols", default=None)
    backtest_parser.add_argument("--start", default=None)
    backtest_parser.add_argument("--end", default=None)
    backtest_parser.add_argument(
        "--agent-mode", choices=["deterministic", "ai", "cached"], default=None
    )
    backtest_parser.add_argument("--cache", default=None)
    backtest_parser.add_argument("--persist", action="store_true", help="persist decisions to DB")

    study_parser = sub.add_parser("study", help="run a parameter sensitivity study")
    study_parser.add_argument("--symbols", default=None)
    study_parser.add_argument("--start", default=None)
    study_parser.add_argument("--end", default=None)
    study_parser.add_argument("--alpha", default=None, help="comma list, e.g. 60,80,100")
    study_parser.add_argument("--stop", default=None, help="comma list")
    study_parser.add_argument("--tp", default=None, help="comma list")
    study_parser.add_argument("--z", default=None, help="comma list")

    ablation_parser = sub.add_parser("ablation", help="compare AI-gated and baseline variants")
    ablation_parser.add_argument("--symbols", default=None)
    ablation_parser.add_argument("--start", default=None)
    ablation_parser.add_argument("--end", default=None)
    ablation_parser.add_argument("--cache", default=None)

    replay_parser = sub.add_parser("replay", help="verify a run from stored decisions/fills")
    replay_parser.add_argument("--run-id", default=None)

    flatten_parser = sub.add_parser("flatten", help="flatten positions (operational)")
    flatten_parser.add_argument("--confirm-live", action="store_true")
    flatten_parser.add_argument("--mode", choices=["shadow", "live"], default="shadow")

    kill_parser = sub.add_parser("kill", help="activate the file kill switch")
    kill_parser.add_argument("--reason", default="manual kill")

    dashboard_parser = sub.add_parser("dashboard", help="run the read-only dashboard")
    dashboard_parser.add_argument("--host", default=None)
    dashboard_parser.add_argument("--port", type=int, default=None)

    health_parser = sub.add_parser("health", help="check trader heartbeat")
    health_parser.add_argument("--max-age", type=float, default=90.0)
    health_parser.add_argument(
        "--mode",
        choices=["shadow", "live", "backtest"],
        default=None,
        help="only consider heartbeats from this mode",
    )

    selftest_parser = sub.add_parser("self-test", help="shadow OMS/risk self-test (no real orders)")
    selftest_parser.add_argument("--ai", action="store_true", help="also call DeepSeek")
    selftest_parser.add_argument("--reflection", action="store_true", help="also test trade reflection")

    sub.add_parser("agent-test", help="call DeepSeek on a synthetic candidate (no orders)")

    review_parser = sub.add_parser(
        "ai-review", help="ask DeepSeek to review historical backtest candidates"
    )
    review_parser.add_argument("--decisions", required=True)
    review_parser.add_argument("--out", required=True)

    sub.add_parser("config", help="print redacted configuration")

    args = parser.parse_args(argv)
    config_dir = args.config_dir

    try:
        if args.command == "run":
            return _run_async(_cmd_run(load_settings(config_dir=config_dir), args.duration, args.confirm_live))
        if args.command == "shadow":
            return _run_async(_cmd_run(load_settings("shadow", config_dir=config_dir), args.duration, False))
        if args.command == "live":
            if not args.confirm_live:
                print("Refused: live mode requires --confirm-live", file=sys.stderr)
                return 2
            return _run_async(_cmd_run(load_settings("live", config_dir=config_dir), args.duration, True))
        if args.command == "preflight":
            settings = load_settings(config_dir=config_dir)
            return _run_async(_cmd_preflight(settings, include_ws=not args.no_ws))
        if args.command == "download":
            settings = load_settings("download", config_dir=config_dir)
            return _run_async(_cmd_download(settings, args))
        if args.command == "backtest":
            settings = load_settings("backtest", config_dir=config_dir)
            return _run_async(_cmd_backtest(settings, args))
        if args.command == "study":
            settings = load_settings("backtest", config_dir=config_dir)
            return _run_async(_cmd_study(settings, args))
        if args.command == "ablation":
            settings = load_settings("backtest", config_dir=config_dir)
            return _run_async(_cmd_ablation(settings, args))
        if args.command == "replay":
            settings = load_settings(config_dir=config_dir)
            return _run_async(_cmd_replay(settings, args.run_id))
        if args.command == "flatten":
            mode = "live" if args.mode == "live" else "shadow"
            if mode == "live" and not args.confirm_live:
                print("Refused: live flatten requires --confirm-live", file=sys.stderr)
                return 2
            settings = load_settings(mode, config_dir=config_dir)
            return _run_async(_cmd_flatten(settings, live=mode == "live"))
        if args.command == "kill":
            settings = load_settings(config_dir=config_dir)
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            settings.kill_file.write_text(args.reason, encoding="utf-8")
            print(json.dumps({"ok": True, "kill_file": str(settings.kill_file), "reason": args.reason}))
            return 0
        if args.command == "dashboard":
            settings = load_settings(config_dir=config_dir)
            import uvicorn

            from wallex_trader.dashboard.api import create_app

            host = args.host or settings.dashboard_host
            port = args.port or settings.dashboard_port
            uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
            return 0
        if args.command == "self-test":
            settings = load_settings("shadow", config_dir=config_dir)
            return _run_async(_cmd_selftest(settings, args.ai, args.reflection))
        if args.command == "agent-test":
            settings = load_settings("shadow", config_dir=config_dir)
            return _run_async(_cmd_selftest(settings, True, False))
        if args.command == "ai-review":
            settings = load_settings("backtest", config_dir=config_dir)
            return _run_async(_cmd_ai_review(settings, args.decisions, args.out))
        if args.command == "health":
            settings = load_settings(config_dir=config_dir)
            return _run_async(_cmd_health(settings, args.max_age, args.mode))
        if args.command == "config":
            settings = load_settings(config_dir=config_dir)
            print(json.dumps(settings.safe_dump(redact=True), indent=2, default=str))
            return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"error: {exc}", file=sys.stderr)
        logging.getLogger("trader.cli").exception("command failed")
        return 1
    return 2


def _run_async(coro: Any) -> int:
    try:
        result = asyncio.run(coro)
    except KeyboardInterrupt:
        return 130
    return int(result or 0)


async def _cmd_run(settings: Settings, duration: float | None, confirm_live: bool) -> int:
    engine = TradingEngine(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await engine.run(duration_sec=duration, confirm_live=confirm_live)
    finally:
        await engine.stop()
    return 0


async def _cmd_preflight(settings: Settings, *, include_ws: bool) -> int:
    setup_logging(settings, component="preflight")
    result = await run_preflight(settings, include_ws=include_ws)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


async def _cmd_download(settings: Settings, args: argparse.Namespace) -> int:
    setup_logging(settings, component="download")
    symbols = _symbols(args.symbols, settings)
    rest = WallexRESTClient(settings)
    try:
        await rest.connect()
        result = await download_history(
            settings,
            rest,
            symbols,
            args.start,
            args.end,
            resolution=args.resolution,
            out_dir=Path(args.out) if args.out else settings.data_dir,
            force=args.force,
        )
    finally:
        await rest.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


async def _cmd_backtest(settings: Settings, args: argparse.Namespace) -> int:
    setup_logging(settings, component="backtest")
    symbols = _symbols(args.symbols, settings)
    start = args.start or settings.backtest_start
    end = args.end or settings.backtest_end
    mode = args.agent_mode or settings.backtest_agent_mode
    settings = settings.model_copy(
        update={
            "backtest_start": start,
            "backtest_end": end,
            "backtest_agent_mode": mode,
            "backtest_cache_file": args.cache or settings.backtest_cache_file,
        }
    )
    db = None
    repo = None
    agent: Any
    if mode == "deterministic":
        agent = DeterministicAgent(settings)
    elif mode == "cached":
        if not args.cache:
            raise ValueError("--cache is required for cached agent mode")
        agent = CachedDecisionAgent(settings, args.cache)
    else:
        db = create_database(settings)
        await db.init()
        run_token = f"btai_{datetime.now(UTC):%Y%m%d_%H%M%S}"
        repo = Repository(db, run_token)
        await repo.start_run(settings, run_token)
        memory = AgentMemory(settings, repo)
        agent = DecisionAgent(
            settings,
            repo,
            memory=memory,
            mcp=None,
        )
    db_created_here = False
    try:
        simulator = BacktestSimulator(settings, symbols, agent=agent)
        result = await simulator.run(start, end)
        if db is None:
            db = create_database(settings)
            await db.init()
            db_created_here = True
        repo = Repository(db, result.run_id)
        await repo.start_run(settings, result.run_id)
        await repo.save_backtest_run(
            run_id=result.run_id,
            config=settings.safe_dump(redact=True),
            metrics=result.metrics,
            artifact_path=result.artifacts["artifact_dir"],
            data_manifest=result.data_manifest,
            git_sha=settings.git_sha or "no-git",
        )
        if args.persist:
            for decision in result.decisions:
                await repo.save_decision({**decision, "run_id": result.run_id})
        await repo.finish_run("finished")
    finally:
        if db is not None and db_created_here:
            await db.close()
        elif db is not None:
            await db.close()
        await agent.close()
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "metrics": result.metrics,
                "artifacts": result.artifacts,
            },
            indent=2,
            default=str,
        )
    )
    return 0


async def _cmd_study(settings: Settings, args: argparse.Namespace) -> int:
    setup_logging(settings, component="study")
    symbols = _symbols(args.symbols, settings)
    start = args.start or settings.backtest_start
    end = args.end or settings.backtest_end
    grid = dict(DEFAULT_GRID)
    if args.alpha:
        grid["alpha_threshold_bps"] = [float(value) for value in args.alpha.split(",")]
    if args.stop:
        grid["stop_loss_bps"] = [float(value) for value in args.stop.split(",")]
    if args.tp:
        grid["take_profit_bps"] = [float(value) for value in args.tp.split(",")]
    if args.z:
        grid["z_entry"] = [float(value) for value in args.z.split(",")]
    result = await run_parameter_study(settings, symbols, start, end, grid=grid)
    print(json.dumps(result, indent=2, default=str))
    return 0


async def _cmd_ablation(settings: Settings, args: argparse.Namespace) -> int:
    setup_logging(settings, component="ablation")
    symbols = _symbols(args.symbols, settings)
    start = args.start or settings.backtest_start
    end = args.end or settings.backtest_end
    result = await run_ablation(settings, symbols, start, end, decision_cache=args.cache)
    print(json.dumps(result, indent=2, default=str))
    return 0


async def _cmd_replay(settings: Settings, run_id: str | None) -> int:
    result = await replay_run(settings, run_id)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


async def _cmd_flatten(settings: Settings, *, live: bool) -> int:
    engine = TradingEngine(settings)
    try:
        await engine.start(confirm_live=live)
        closed = await engine._flatten_all("manual_flatten")
        print(json.dumps({"ok": True, "closed": closed, "mode": settings.mode.value}))
    finally:
        await engine.stop()
    return 0


async def _cmd_selftest(settings: Settings, include_ai: bool, include_reflection: bool) -> int:
    setup_logging(settings, component="selftest")
    from wallex_trader.selftest import run_self_test

    result = await run_self_test(
        settings, include_ai=include_ai, include_reflection=include_reflection
    )
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


async def _cmd_ai_review(settings: Settings, decisions: str, out: str) -> int:
    setup_logging(settings, component="ai_review")
    from wallex_trader.backtest.ai_review import review_backtest_candidates

    result = await review_backtest_candidates(settings, decisions, out)
    print(json.dumps(result, indent=2, default=str))
    return 0


async def _cmd_health(settings: Settings, max_age: float, mode: str | None = None) -> int:
    db = create_database(settings)
    await db.init()
    repo = Repository(db, "health")
    try:
        heartbeat = await repo.latest_heartbeat(mode=mode)
        healthy = False
        age = None
        if heartbeat and heartbeat.get("ts"):
            try:
                parsed = datetime.fromisoformat(str(heartbeat["ts"]))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - parsed).total_seconds()
                healthy = age <= max_age
            except ValueError:
                age = None
        print(
            json.dumps(
                {
                    "ok": healthy,
                    "mode": settings.mode.value,
                    "filter_mode": mode,
                    "heartbeat_age_sec": round(age, 1) if age is not None else None,
                    "max_age_sec": max_age,
                    "db": str(settings.postgres_dsn).split("@")[-1],
                },
                indent=2,
            )
        )
        return 0 if healthy else 1
    finally:
        await db.close()


def _symbols(value: str | None, settings: Settings) -> list[str]:
    if value:
        return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
    return settings.universe_or_default()


if __name__ == "__main__":
    raise SystemExit(main())
