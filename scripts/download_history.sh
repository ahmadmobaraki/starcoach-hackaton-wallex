#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then PY=python3; fi
START="${1:-2026-05-01}"
END="${2:-2026-10-21}"
exec "$PY" -m wallex_trader download --start "$START" --end "$END"
