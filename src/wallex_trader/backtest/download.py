from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from wallex_trader.config import Settings
from wallex_trader.exchange.rest import WallexAPIError, WallexRESTClient
from wallex_trader.models import Candle

logger = logging.getLogger("trader.download")


@dataclass(slots=True)
class DownloadResult:
    symbol: str
    days_requested: int = 0
    days_ok: int = 0
    days_empty: int = 0
    days_failed: int = 0
    rows: int = 0
    gaps: int = 0
    manifest: dict[str, Any] = field(default_factory=dict)


def _parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


async def download_history(
    settings: Settings,
    rest: WallexRESTClient,
    symbols: list[str],
    start: str | date | datetime,
    end: str | date | datetime,
    *,
    resolution: str = "1",
    out_dir: Path | None = None,
    force: bool = False,
    chunk_days: int | None = None,
) -> dict[str, Any]:
    start_day = _parse_date(start)
    end_day = _parse_date(end)
    if end_day < start_day:
        raise ValueError("end must be after start")
    out = out_dir or settings.data_dir
    raw_root = out / "raw"
    curated_root = out / "curated" / "candles"
    manifest_path = out / "manifests" / "download_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest(manifest_path)
    step = chunk_days or (1 if resolution == "1" else 30)
    all_results: dict[str, DownloadResult] = {}
    gap_report: dict[str, Any] = {}

    for symbol in symbols:
        result = DownloadResult(symbol=symbol)
        trades: dict[int, tuple[str, int, int]] = {}
        cursor = start_day
        while cursor <= end_day:
            chunk_end = min(end_day, cursor + timedelta(days=step - 1))
            key = f"{symbol}:{resolution}:{cursor.isoformat()}:{chunk_end.isoformat()}"
            parquet_path = curated_root / symbol / resolution / f"{cursor.isoformat()}.parquet"
            raw_path = raw_root / symbol / resolution / f"{cursor.isoformat()}.json.gz"
            result.days_requested += 1
            if parquet_path.exists() and not force:
                try:
                    existing = pd.read_parquet(parquet_path)
                    result.rows += len(existing)
                    result.days_ok += 1
                    _gaps_from_frame(existing, result)
                    trades[int(cursor.toordinal())] = (
                        "cached",
                        len(existing),
                        int(pd.to_datetime(existing["ts"], unit="s", utc=True).iloc[0].timestamp())
                        if len(existing)
                        else 0,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cached parquet unreadable %s: %s", parquet_path, exc)
                cursor = chunk_end + timedelta(days=1)
                continue
            try:
                candles = await _fetch_chunk(rest, symbol, resolution, cursor, chunk_end)
            except WallexAPIError as exc:
                result.days_failed += 1
                manifest[key] = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "from": cursor.isoformat(),
                    "to": chunk_end.isoformat(),
                    "status": "error",
                    "error": str(exc)[:500],
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
                _write_manifest(manifest_path, manifest)
                logger.warning("download failed %s %s: %s", symbol, cursor, exc)
                cursor = chunk_end + timedelta(days=1)
                continue
            if not candles:
                result.days_empty += 1
                manifest[key] = {
                    "symbol": symbol,
                    "resolution": resolution,
                    "from": cursor.isoformat(),
                    "to": chunk_end.isoformat(),
                    "status": "no_data",
                    "rows": 0,
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
                _write_manifest(manifest_path, manifest)
                cursor = chunk_end + timedelta(days=1)
                continue
            frame = _candles_to_frame(symbol, candles)
            validation = validate_frame(frame)
            parquet_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            frame.to_parquet(parquet_path, index=False)
            raw_payload = {
                "symbol": symbol,
                "resolution": resolution,
                "from": _day_start(cursor).isoformat(),
                "to": chunk_end.isoformat(),
                "rows": len(frame),
                "columns": list(frame.columns),
                "data": frame.to_dict(orient="list"),
            }
            raw_path.write_bytes(gzip.compress(json.dumps(raw_payload).encode()))
            checksum = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            result.rows += len(frame)
            result.days_ok += 1
            result.gaps += validation["gaps_1m"]
            manifest[key] = {
                "symbol": symbol,
                "resolution": resolution,
                "from": cursor.isoformat(),
                "to": chunk_end.isoformat(),
                "status": "ok",
                "rows": len(frame),
                "first_ts": int(frame["ts"].iloc[0]),
                "last_ts": int(frame["ts"].iloc[-1]),
                "checksum": checksum,
                "parquet": str(parquet_path),
                "raw": str(raw_path),
                "validation": validation,
                "fetched_at": datetime.now(UTC).isoformat(),
            }
            _write_manifest(manifest_path, manifest)
            trades[int(cursor.toordinal())] = ("ok", len(frame), int(frame["ts"].iloc[0]))
            logger.info("downloaded %s %s rows=%d gaps=%d", symbol, cursor, len(frame), validation["gaps_1m"])
            cursor = chunk_end + timedelta(days=1)
        all_results[symbol] = result
        gap_report[symbol] = {
            "days_ok": result.days_ok,
            "days_empty": result.days_empty,
            "days_failed": result.days_failed,
            "rows": result.rows,
            "gaps_1m": result.gaps,
        }
    (out / "manifests" / "gap_report.json").write_text(
        json.dumps(gap_report, indent=2), encoding="utf-8"
    )
    return {
        "symbols": {
            symbol: {
                "days_requested": result.days_requested,
                "days_ok": result.days_ok,
                "days_empty": result.days_empty,
                "days_failed": result.days_failed,
                "rows": result.rows,
                "gaps_1m": result.gaps,
            }
            for symbol, result in all_results.items()
        },
        "manifest": str(manifest_path),
        "gap_report": gap_report,
    }


async def _fetch_chunk(
    rest: WallexRESTClient,
    symbol: str,
    resolution: str,
    start_day: date,
    end_day: date,
) -> list[Candle]:
    start = _day_start(start_day)
    end = _day_start(end_day + timedelta(days=1))
    now = datetime.now(UTC)
    if end > now:
        end = now
    if start >= end:
        return []
    return await rest.candles(symbol, resolution=resolution, start=start, end=end)


def _candles_to_frame(symbol: str, candles: list[Candle]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": int(candle.ts.timestamp()),
                "symbol": symbol,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]
    ).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def validate_frame(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {"rows": 0, "duplicates": 0, "invalid_ohlc": 0, "negative_volume": 0, "gaps_1m": 0, "monotonic": True}
    duplicates = int(frame["ts"].duplicated().sum())
    invalid = int(
        (
            (frame["low"] > frame[["open", "close"]].min(axis=1))
            | (frame["high"] < frame[["open", "close"]].max(axis=1))
            | (frame["high"] < frame["low"])
        ).sum()
    )
    negative = int((frame["volume"] < 0).sum())
    diffs = frame["ts"].diff().dropna()
    gaps = int((diffs > 60).sum())
    monotonic = bool(frame["ts"].is_monotonic_increasing)
    return {
        "rows": len(frame),
        "duplicates": duplicates,
        "invalid_ohlc": invalid,
        "negative_volume": negative,
        "gaps_1m": gaps,
        "monotonic": monotonic,
    }


def _gaps_from_frame(frame: pd.DataFrame, result: DownloadResult) -> None:
    if len(frame) > 1:
        diffs = frame["ts"].diff().dropna()
        result.gaps += int((diffs > 60).sum())


def _load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_candles(
    settings: Settings,
    symbols: list[str],
    start: str | date | datetime | None = None,
    end: str | date | datetime | None = None,
    *,
    resolution: str = "1",
    data_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    root = (data_dir or settings.data_dir) / "curated" / "candles"
    start_day = _parse_date(start) if start else None
    end_day = _parse_date(end) if end else None
    result: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        symbol_dir = root / symbol / resolution
        if not symbol_dir.exists():
            logger.warning("no parquet data for %s at %s", symbol, symbol_dir)
            continue
        frames = []
        for path in sorted(symbol_dir.glob("*.parquet")):
            try:
                day = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if start_day and day < start_day:
                continue
            if end_day and day > end_day:
                continue
            frame = pd.read_parquet(path)
            if not frame.empty:
                frames.append(frame)
        if not frames:
            continue
        combined = (
            pd.concat(frames, ignore_index=True)
            .drop_duplicates("ts")
            .sort_values("ts")
            .reset_index(drop=True)
        )
        combined["dt"] = pd.to_datetime(combined["ts"], unit="s", utc=True)
        combined = combined.set_index("dt")
        result[symbol] = combined
    return result


def data_manifest(settings: Settings, symbols: list[str]) -> dict[str, Any]:
    path = settings.data_dir / "manifests" / "download_manifest.json"
    manifest = _load_manifest(path)
    return {
        "manifest_path": str(path),
        "entries": len(manifest),
        "symbols": symbols,
        "manifest_sha": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
        if manifest
        else "",
    }


async def download_and_report(
    settings: Settings,
    rest: WallexRESTClient,
    symbols: list[str],
    start: str,
    end: str,
) -> dict[str, Any]:
    result = await download_history(settings, rest, symbols, start, end)
    await asyncio.sleep(0)
    return result
