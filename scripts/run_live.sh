#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then PY=python3; fi
MODE=live "$PY" -m wallex_trader preflight
exec "$PY" -m wallex_trader live --confirm-live "$@"
