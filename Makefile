SHELL := /bin/bash
VENV := .venv
PY := $(VENV)/bin/python
UV := uv
COMPOSE := docker compose

.DEFAULT_GOAL := help

.PHONY: help install lock lint typecheck test test-live preflight download backtest study ablation shadow live replay up down build logs dashboard self-test agent-test flatten kill proxy

help:
	@echo "ARGUS commands:"
	@echo "  make install       create .venv and install dependencies"
	@echo "  make test          unit test suite"
	@echo "  make lint          ruff"
	@echo "  make typecheck     mypy"
	@echo "  make preflight     REST/WS/MCP/DeepSeek/account checks (no orders)"
	@echo "  make download      backfill 1m candles for the configured universe"
	@echo "  make backtest      run the frozen backtest config"
	@echo "  make study         parameter sensitivity study"
	@echo "  make ablation      AI-gated vs baseline comparison"
	@echo "  make shadow        live-data shadow mode (no real orders)"
	@echo "  make live          live mode (requires LIVE_CONFIRM + canary)"
	@echo "  make replay        verify a stored run"
	@echo "  make up / down     Docker Compose stack"
	@echo "  make proxy         optional Caddy reverse proxy profile"
	@echo "  make self-test     deterministic shadow OMS/risk test"
	@echo "  make agent-test    DeepSeek decision on a synthetic candidate"
	@echo "  make flatten       flatten positions (shadow by default)"
	@echo "  make kill          activate the file kill switch"

install:
	$(UV) sync --frozen --extra dev

lock:
	$(UV) lock

lint:
	$(PY) -m ruff check src tests

typecheck:
	$(PY) -m mypy src/wallex_trader

test:
	$(PY) -m pytest

test-live:
	RUN_LIVE_TESTS=1 $(PY) -m pytest -m live

preflight:
	$(PY) -m wallex_trader preflight

download:
	$(PY) -m wallex_trader download --start "$${BACKTEST_START:-2026-05-01}" --end "$${BACKTEST_END:-2026-10-21}"

backtest:
	$(PY) -m wallex_trader backtest

study:
	$(PY) -m wallex_trader study

ablation:
	$(PY) -m wallex_trader ablation

ai-review:
	$(PY) -m wallex_trader ai-review --decisions "$${DECISIONS:?set DECISIONS=path}" --out "$${AI_CACHE:-artifacts/ablations/ai_cache.jsonl}"

shadow:
	$(PY) -m wallex_trader shadow

live:
	$(PY) -m wallex_trader live --confirm-live

replay:
	$(PY) -m wallex_trader replay

dashboard:
	$(PY) -m wallex_trader dashboard

self-test:
	$(PY) -m wallex_trader self-test

agent-test:
	$(PY) -m wallex_trader agent-test

flatten:
	$(PY) -m wallex_trader flatten

kill:
	$(PY) -m wallex_trader kill --reason "make kill"

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200 trader dashboard

proxy:
	$(COMPOSE) --profile proxy up -d caddy

.PHONY: tools
tools:
	$(COMPOSE) --profile tools run --rm downloader
