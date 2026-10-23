# Shadow testing

## What shadow mode is

Shadow mode uses live Wallex market data, the real DeepSeek agent, the real risk
kernel, the real OMS and the real portfolio accounting. Only the broker changes:
`ShadowBroker` never calls the Wallex order endpoint. It simulates fills
pessimistically:

- MARKETABLE_LIMIT walks the live book up to the limit, then adds an impact/slippage
  charge and pays the taker fee;
- LIMIT orders wait; a passive buy fills only when the market trades strictly through
  the limit (or the book crosses); maker fee applies;
- partial fills and timeout/cancel/reprice are supported;
- self-trade prevention is enforced;
- fees are charged in TMN;
- a kill switch/flatten acts on the simulated portfolio.

The same `MarketDataFeed`, `SwingSignalEngine`, `DecisionAgent`, `RiskKernel`, `OMS`
and `Portfolio` classes are used in live mode. There is no separate “shadow strategy”.

## Safety proof

- `ShadowBroker` has `is_live = False` and no REST order call.
- `OMS` refuses a `LiveBroker` in shadow and a `ShadowBroker` in live.
- `TradingEngine` refuses live mode without `--confirm-live` and the exact
  `LIVE_CONFIRM` phrase.
- The dashboard has no order-entry controls; the only mutation endpoint is the
  token-protected kill switch.

## Run it

```bash
make preflight                 # real REST/WS/MCP/DeepSeek/account checks
make self-test                 # deterministic shadow OMS/risk test
make dashboard                 # open http://127.0.0.1:8080/test-results
make shadow                    # starts live-data shadow mode
```

Or a bounded smoke run:

```bash
python -m wallex_trader shadow --duration 150
```

Artifacts appear in `artifacts/shadow/<run_id>/`:

- `metrics.json` — equity, snapshot, trades, decision count, agent/reflection stats;
- `decisions.jsonl` — every candidate with AI/risk/execution context;
- `agent_journal.md` — copy of the learning journal at shutdown.

The dashboard's **Shadow** tab lists all runs; **Agent memory** shows the lessons.

## Evidence from this workspace

### Preflight (real APIs)

- Wallex markets: 469, depth and trades OK, 1,441 1m candles/day OK.
- Account: key valid; the main account holds 0 TMN, but the prop sub-account is funded
  with **5,000,000 TMN**. The prop account's API key is in `.env`; live preflight passes
  (`api_key_matches_sub_account=true`). The first main-key attempt is documented in
  `report.md` §7.4.
- Fees: BTCTMN 0.25% maker / 0.30% taker.
- Clock skew: ~0.019s.
- WebSocket: connected and received depth/candle pushes after transient 503 reconnects.
- Wallex MCP: 52 tools, initialize/tools/list OK.
- DeepSeek: JSON response OK, model `deepseek-flash`, ~1.9s.
- The full JSON is under `artifacts/preflight/`.

### Deterministic shadow self-test

`make self-test` builds a synthetic BTC candidate, runs the real risk kernel, opens a
paper position through the real OMS/ShadowBroker and exits it. The result is a small
loss (as expected from a tight synthetic round trip) with correct accounting:
3.74715216 BTC at 100,048 → 99,972, entry/exit fees ~2,249 TMN, equity 4,997,467 TMN.
It proves the sizing, fill, fee and PnL plumbing. No network order is sent.

### Swing shadow runs (live market data)

Final 110s run (`shadow_20261022_120816_2oxj`):

- 8 symbols selected.
- 3 AI decisions at the first completed 4h bar: USDTTMN, BTCTMN, XAUTTMN.
- AI chose `size_fraction=0.75` and `MARKETABLE_LIMIT` for USDTTMN, approving a 1.03
  USDTT paper fill (~278,669 TMN). It chose HOLD for BTC and XAUT after seeing the
  other candidates and the regime/portfolio context, explicitly preferring the stronger
  USDTT trend and avoiding correlated exposure.
- The position opened with a 10% hard stop and 25% target; no real orders.
- The run stopped cleanly; equity, positions, decision log and shadow metrics were
  persisted.

Earlier runs filled PEPE/BTC and exercised a passive USDTT order that was repriced
twice then cancelled. All runs exercised the real AI, risk kernel and OMS path.

Earlier runs caught two real bugs that were then fixed: Wallex's `tickSize`/`stepSize`
fields are decimal-place counts on some markets (fixed by using precision), and
universe selection was too strict on a transiently wide book (fixed by checking the
book at order time).

### Learning loop

`make self-test --reflection` produced a real DeepSeek reflection and journal note.
The note correctly identified the synthetic instant-exit trade as a test and advised
requiring a minimum holding period and a 2× cost margin. It is stored outside the
main journal (`artifacts/selftest/`) so it cannot bias live decisions.

## Recommended 3-day shadow procedure

1. Set `CHALLENGE_START`/`CHALLENGE_END` if the official window is known.
2. Run preflight and `make self-test`.
3. Start shadow with `make shadow` for the full 72 hours (or the shadow window).
4. Keep the dashboard open; check heartbeat, WS reconnects, LLM errors, risk events.
5. At the end, open `artifacts/shadow/<run_id>/metrics.json` and the report/notes.
6. Run `python -m wallex_trader replay --run-id <run_id>` and verify PASS.
7. Only then consider a tiny live canary, if the account is funded and the challenge
   window allows it.

## Acceptance checklist

- [x] Public REST depth/trades/candles work locally.
- [x] Private balances/fees/open orders work with the provided key.
- [x] WebSocket connects, reconnects and resyncs.
- [x] MCP initialize/tools/list works.
- [x] DeepSeek JSON decision and reflection work.
- [x] Shadow OMS/risk self-test opens and closes a paper trade.
- [x] Shadow swing run makes AI decisions and paper fills without real orders.
- [x] Kill switch, de-risking, stops, trailing stops and endgame code paths exist.
- [x] Dashboard renders runs, decisions, positions, notes, backtests and audit.
- [ ] Live canary — not attempted during implementation; the funded prop sub-account is ready.

## Activity note

The default slow trend strategy trades less than an intraday scalper. In a 150s
shadow window it opened two positions; over three days it may open only a handful.
That is intentional: each round trip costs ~55–90 bps, so forcing activity would turn
a positive-expectancy swing system into a negative-expectancy one. The **Activity**
score is 15 points; the **Profit** score is 90. If the judges require more activity,
increase the universe to 12 symbols (data and backtest are supported) or lower the
horizon after re-running the cost stress; do not enable the losing intraday baseline.
