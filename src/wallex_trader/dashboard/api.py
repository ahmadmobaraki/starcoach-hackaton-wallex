from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from wallex_trader.config import Settings, load_settings
from wallex_trader.dashboard.views import STATIC_DIR, templates
from wallex_trader.storage.db import create_database
from wallex_trader.storage.repositories import Repository

DASHBOARD_RUN_ID = "dashboard"


class KillRequest(BaseModel):
    confirm: str
    reason: str = "dashboard kill"


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or load_settings()
    db = create_database(app_settings)
    repo = Repository(db, DASHBOARD_RUN_ID)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await db.init()
        yield
        await db.close()

    app = FastAPI(
        title="ARGUS — Wallex agentic trading engine",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.db = db
    app.state.repo = repo
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz":
            return await call_next(request)
        password = app_settings.dashboard_basic_auth_password.get_secret_value()
        if not password:
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("basic "):
            return Response(
                status_code=401,
                content="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="ARGUS"'},
            )
        import base64

        try:
            decoded = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
            username, supplied = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            username, supplied = "", ""
        expected_user = app_settings.dashboard_basic_auth_user
        if not (
            secrets.compare_digest(username, expected_user)
            and secrets.compare_digest(supplied, password)
        ):
            return Response(
                status_code=401,
                content="Invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="ARGUS"'},
            )
        return await call_next(request)

    @app.get("/")
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/test-results")

    # ---- health -------------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> Response:
        db_ok = await db.healthcheck()
        heartbeat = await repo.latest_heartbeat()
        heartbeat_age = None
        if heartbeat and heartbeat.get("ts"):
            try:
                ts = datetime.fromisoformat(str(heartbeat["ts"]))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                heartbeat_age = (datetime.now(UTC) - ts).total_seconds()
            except ValueError:
                heartbeat_age = None
        trader_healthy = bool(heartbeat_age is not None and heartbeat_age < 90)
        payload = {
            "status": "ok" if db_ok else "error",
            "database": db_ok,
            "trader_healthy": trader_healthy,
            "heartbeat_age_sec": round(heartbeat_age, 1) if heartbeat_age is not None else None,
            "mode": app_settings.mode.value,
        }
        return JSONResponse(payload, status_code=200 if db_ok else 503)

    # ---- APIs ---------------------------------------------------------
    @app.get("/api/overview")
    async def api_overview(run_id: str | None = None) -> dict[str, Any]:
        data = await repo.overview(run_id)
        active = data.get("run") or {}
        active_id = active.get("run_id") if active else None
        data["equity_series"] = (
            await repo.equity_series(active_id, 600) if active_id else []
        )
        return data

    @app.get("/api/equity")
    async def api_equity(
        run_id: str | None = None, limit: int = Query(default=1000, ge=1, le=10000)
    ) -> list[dict[str, Any]]:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        if not active:
            return []
        return await repo.equity_series(active, limit)

    @app.get("/api/positions")
    async def api_positions(run_id: str | None = None) -> list[dict[str, Any]]:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        return await repo.latest_positions(active) if active else []

    @app.get("/api/orders")
    async def api_orders(run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        return await repo.list_orders(active, limit) if active else []

    @app.get("/api/decisions")
    async def api_decisions(
        run_id: str | None = None,
        symbol: str | None = None,
        action: str | None = None,
        limit: int = Query(default=100, ge=1, le=2000),
    ) -> list[dict[str, Any]]:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        if not active:
            return []
        rows = await repo.recent_decisions(active, symbol=symbol, limit=limit)
        if action:
            rows = [row for row in rows if row["action"].upper() == action.upper()]
        return rows

    @app.get("/api/trades")
    async def api_trades(run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        return await repo.list_fills(active, limit) if active else []

    @app.get("/api/agent/notes")
    async def api_notes(run_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return await repo.list_agent_notes(run_id, limit)

    @app.get("/api/backtests")
    async def api_backtests() -> list[dict[str, Any]]:
        return await repo.list_backtests()

    @app.get("/api/backtests/{run_id}")
    async def api_backtest(run_id: str) -> dict[str, Any]:
        row = await repo.get_backtest(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="backtest not found")
        return row

    @app.get("/api/backtests/{run_id}/report")
    async def api_backtest_report(run_id: str) -> HTMLResponse:
        row = await repo.get_backtest(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="backtest not found")
        path = Path(str(row.get("artifact_path", ""))) / "report.html"
        report = _safe_artifact_path(path, app_settings.artifacts_dir)
        if not report.exists():
            raise HTTPException(status_code=404, detail="report artifact missing")
        return HTMLResponse(report.read_text(encoding="utf-8"))

    @app.get("/api/shadow")
    async def api_shadow_runs() -> list[dict[str, Any]]:
        return await repo.list_shadow_runs()

    @app.get("/api/shadow/{run_id}")
    async def api_shadow_run(run_id: str) -> dict[str, Any]:
        for row in await repo.list_shadow_runs(200):
            if row["run_id"] == run_id:
                return row
        raise HTTPException(status_code=404, detail="shadow run not found")

    @app.get("/api/system")
    async def api_system() -> dict[str, Any]:
        run = await repo.latest_trading_run()
        active = run["run_id"] if run else None
        return {
            "run": run,
            "heartbeat": await repo.latest_heartbeat(),
            "risk_events": await repo.list_risk_events(active, 100) if active else [],
            "system_events": await repo.list_system_events(active, 100),
            "config": app_settings.safe_dump(redact=True),
            "kill_switch": {
                "file": str(app_settings.kill_file),
                "present": app_settings.kill_file.exists(),
            },
        }

    @app.get("/api/export/decisions.jsonl")
    async def api_export_decisions(run_id: str | None = None) -> PlainTextResponse:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        if not active:
            return PlainTextResponse("")
        body = await repo.export_decisions_jsonl(active)
        return PlainTextResponse(
            body,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{active}-decisions.jsonl"'},
        )

    @app.get("/api/stream")
    async def api_stream(request: Request) -> StreamingResponse:
        async def generator():
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await repo.overview()
                    yield f"data: {json.dumps(data, default=str)}\n\n"
                except Exception as exc:  # noqa: BLE001
                    yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.post("/api/kill")
    async def api_kill(request: Request, body: KillRequest) -> dict[str, Any]:
        expected = app_settings.dashboard_kill_token.get_secret_value()
        if not expected:
            raise HTTPException(status_code=503, detail="kill token is not configured")
        supplied = request.headers.get("x-kill-token", "")
        if not secrets.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail="invalid kill token")
        required = "KILL_LIVE" if app_settings.is_live else "KILL"
        if body.confirm != required:
            raise HTTPException(
                status_code=400,
                detail=f"confirmation body must be {{'confirm': '{required}'}}",
            )
        app_settings.kill_file.parent.mkdir(parents=True, exist_ok=True)
        app_settings.kill_file.write_text(body.reason or "dashboard kill", encoding="utf-8")
        await repo.system_event(
            "critical",
            "dashboard",
            f"kill switch requested: {body.reason}",
            {"mode": app_settings.mode.value, "confirm": body.confirm},
        )
        return {"ok": True, "mode": app_settings.mode.value, "kill_file": str(app_settings.kill_file)}

    # ---- pages --------------------------------------------------------
    @app.get("/test-results", response_class=HTMLResponse)
    async def page_overview(request: Request, run_id: str | None = None) -> HTMLResponse:
        data = await repo.overview(run_id)
        active = data.get("run") or {}
        active_id = active.get("run_id") if active else None
        equity = await repo.equity_series(active_id, 600) if active_id else []
        equity_values = [_num(row.get("equity_tmn")) for row in equity]
        drawdown_values = [round(_num(row.get("drawdown_pct")) * 100, 3) for row in equity]
        labels = [str(row.get("ts", ""))[:19] for row in equity]
        return templates.TemplateResponse(
            request=request,
            name="overview.html",
            context={
                "request": request,
                "settings": app_settings,
                "data": data,
                "run": active,
                "equity": equity,
                "equity_labels": labels,
                "equity_values": equity_values,
                "drawdown_values": drawdown_values,
            },
        )

    @app.get("/test-results/decisions", response_class=HTMLResponse)
    async def page_decisions(
        request: Request,
        symbol: str | None = None,
        action: str | None = None,
        run_id: str | None = None,
    ) -> HTMLResponse:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        rows = await repo.recent_decisions(active, symbol=symbol, limit=500) if active else []
        if action:
            rows = [row for row in rows if row["action"].upper() == action.upper()]
        return templates.TemplateResponse(
            request=request,
            name="decisions.html",
            context={
                "request": request,
                "settings": app_settings,
                "run": await repo.get_run(active) if active else None,
                "decisions": rows,
                "symbol": symbol,
                "action": action,
            },
        )

    @app.get("/test-results/positions", response_class=HTMLResponse)
    async def page_positions(request: Request, run_id: str | None = None) -> HTMLResponse:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        return templates.TemplateResponse(
            request=request,
            name="positions.html",
            context={
                "request": request,
                "settings": app_settings,
                "run": await repo.get_run(active) if active else None,
                "positions": await repo.latest_positions(active) if active else [],
                "orders": await repo.list_orders(active, 100) if active else [],
                "fills": await repo.list_fills(active, 100) if active else [],
            },
        )

    @app.get("/test-results/memory", response_class=HTMLResponse)
    async def page_memory(request: Request, run_id: str | None = None) -> HTMLResponse:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        journal = ""
        if app_settings.journal_path.exists():
            journal = app_settings.journal_path.read_text(encoding="utf-8")[-80_000:]
        return templates.TemplateResponse(
            request=request,
            name="memory.html",
            context={
                "request": request,
                "settings": app_settings,
                "run": await repo.get_run(active) if active else None,
                "notes": await repo.list_agent_notes(active, 200) if active else [],
                "journal": journal,
            },
        )

    @app.get("/test-results/backtests", response_class=HTMLResponse)
    async def page_backtests(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="backtests.html",
            context={
                "request": request,
                "settings": app_settings,
                "backtests": await repo.list_backtests(),
            },
        )

    @app.get("/test-results/backtests/{run_id}", response_class=HTMLResponse)
    async def page_backtest_detail(request: Request, run_id: str) -> HTMLResponse:
        row = await repo.get_backtest(run_id)
        if not row:
            raise HTTPException(status_code=404, detail="backtest not found")
        return templates.TemplateResponse(
            request=request,
            name="backtest_detail.html",
            context={"request": request, "settings": app_settings, "backtest": row},
        )

    @app.get("/test-results/shadow", response_class=HTMLResponse)
    async def page_shadow(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="shadow.html",
            context={
                "request": request,
                "settings": app_settings,
                "runs": await repo.list_shadow_runs(),
            },
        )

    @app.get("/test-results/system", response_class=HTMLResponse)
    async def page_system(request: Request) -> HTMLResponse:
        run = await repo.latest_trading_run()
        active = run["run_id"] if run else None
        return templates.TemplateResponse(
            request=request,
            name="system.html",
            context={
                "request": request,
                "settings": app_settings,
                "run": run,
                "heartbeat": await repo.latest_heartbeat(),
                "risk_events": await repo.list_risk_events(active, 100) if active else [],
                "system_events": await repo.list_system_events(active, 100),
                "config": app_settings.safe_dump(redact=True),
            },
        )

    @app.get("/test-results/audit", response_class=HTMLResponse)
    async def page_audit(request: Request, run_id: str | None = None) -> HTMLResponse:
        active = run_id or (await repo.latest_trading_run() or {}).get("run_id")
        return templates.TemplateResponse(
            request=request,
            name="audit.html",
            context={
                "request": request,
                "settings": app_settings,
                "run": await repo.get_run(active) if active else None,
                "decisions": await repo.recent_decisions(active, limit=1000) if active else [],
            },
        )

    return app


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_artifact_path(path: Path, root: Path) -> Path:
    try:
        resolved = path.resolve()
        resolved.relative_to(Path(root).resolve())
        return resolved
    except (OSError, ValueError):
        raise HTTPException(status_code=403, detail="artifact path outside artifacts directory") from None
