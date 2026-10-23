#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PYTHON:-.venv/bin/python}"
if [[ ! -x "$PY" ]]; then PY=python3; fi
echo "== public/private/WS/MCP/DeepSeek preflight =="
"$PY" -m wallex_trader preflight
echo "== deterministic shadow OMS/risk test =="
"$PY" -m wallex_trader self-test
echo "== DeepSeek decision test (no orders) =="
"$PY" -m wallex_trader agent-test
