from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from wallex_trader.config import Settings

_SECRET_MARKERS = ("api_key", "apikey", "secret", "token", "password", "authorization")
_RUN_CONTEXT: dict[str, str | None] = {"run_id": None, "mode": None}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if _RUN_CONTEXT.get("run_id"):
            payload["run_id"] = _RUN_CONTEXT["run_id"]
            payload["mode"] = _RUN_CONTEXT.get("mode")
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update({str(k): _redact(v) for k, v in extra.items()})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if any(marker in str(k).lower() for marker in _SECRET_MARKERS) else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def attach_run_log(settings: Settings, run_id: str, mode: str) -> Path:
    """Add a per-run JSONL file and stamp run_id/mode on every log line."""
    _RUN_CONTEXT["run_id"] = run_id
    _RUN_CONTEXT["mode"] = mode
    run_dir = Path(settings.logs_dir) / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{run_id}.jsonl"
    handler = RotatingFileHandler(
        path,
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    logging.getLogger("trader").info(
        "per-run log attached", extra={"context": {"log_path": str(path)}}
    )
    return path


def setup_logging(settings: Settings, component: str = "trader") -> logging.Logger:
    settings.ensure_dirs()
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)

    log_path = Path(settings.logs_dir) / f"{component}.jsonl"
    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=20 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logger = logging.getLogger(component)
    logger.info(
        "logging configured",
        extra={"context": {"component": component, "log_path": str(log_path)}},
    )
    return logger


def log(
    logger: logging.Logger,
    level: int,
    message: str,
    **context: Any,
) -> None:
    logger.log(level, message, extra={"context": context})


def info(logger: logging.Logger, message: str, **context: Any) -> None:
    log(logger, logging.INFO, message, **context)


def warning(logger: logging.Logger, message: str, **context: Any) -> None:
    log(logger, logging.WARNING, message, **context)


def error(logger: logging.Logger, message: str, **context: Any) -> None:
    log(logger, logging.ERROR, message, **context)
