# Compliance and safety boundaries

## Market rules

- **Spot only.** The system only sends spot BUY/SELL LIMIT orders on Wallex TMN
  markets. It does not use margin, futures, leverage, options, or borrowing.
- **Long only.** BUY opens or adds; SELL is only allowed to reduce an existing spot
  position. There is no short-selling path in the code.
- **Allowlist.** Only `UNIVERSE_ALLOWLIST` symbols can be traded. The default is eight
  liquid TMN markets; adding a symbol requires an explicit config change.
- **No withdrawals/transfers.** The API key is used for balances, fees, orders and
  cancels. There is no withdrawal, transfer, IBAN, card, credit or sub-account
  management code path. State-changing MCP tools are blocked by the agent allowlist.
- **No wash/self-trade.** The risk kernel and both brokers reject an opposite open
  order on the same symbol. The MCP allowlist excludes order creation/cancel.
- **Order hygiene.** Client IDs are unique, orders are persisted before sending,
  ambiguous POSTs are queried rather than blindly retried, quantities are floored to
  the exchange step and prices quantized to the tick.
- **Rate limits.** Public REST is token-bucketed; orders use a rolling 10s budget
  (15 default, below Wallex's documented limit). The feed uses one WebSocket and
  REST fallback.

## AI boundaries

- The AI can choose BUY/SELL/HOLD, size within the approved range, order type/offset,
  stop/TP/horizon within bounds, and normal exits.
- The AI cannot change hard limits, the allowlist, leverage, the broker, or code.
- The AI cannot withdraw, transfer, add IBANs/cards, or create sub-accounts.
- The AI's lessons are advisory; the risk kernel ignores any lesson that conflicts
  with a hard limit.
- The deterministic risk kernel can veto or force exits. A HOLD is always safe.
- Prompts treat market text and notes as untrusted data, not instructions.
- The AI cannot call state-changing MCP tools; only read-only market/account tools are
  allowlisted.

## Human oversight

- The dashboard is read-only except for the token-protected kill switch.
- There is no manual order-entry button.
- The kill switch is sticky and requires a deliberate restart.
- Live mode requires an exact confirmation phrase and a CLI flag, and refuses to start
  with a 0/insufficient balance or stale open orders.
- Preflight must pass before live; the canary config limits order size.
- A human should inspect the first live fills and reconciliation before scaling.

## Data and privacy

- API keys and secrets are loaded from `.env`/environment and never logged in clear
  text. Structured logs redact fields matching key/secret/token/password markers.
- The dashboard displays a redacted config; prompts and responses are stored without
  keys.
- The agent journal and DB contain trading data, not personal data. Back up and access
  them like trading records.

## Challenge alignment

| Criterion | How ARGUS addresses it |
|---|---|
| Final profit | Positive 113-day backtest (+15.79% baseline); swing thesis after measured fees |
| Loss control | 10% stops, 3% heat, 2.5% daily loss, 6% drawdown kill, 3%/4.5% de-risking |
| Algorithm activity | 38–42 trades/113 days; slow trend, not forced scalping; universe can be expanded |
| Execution stability | Retries, idempotent client IDs, reconciliation, kill switch, tests, shadow runs |
| Strategy logic | Documented 8d/40d EMA, 24h momentum, ATR sizing, cost hurdle, exits |
| Real AI use | AI vetoes, sizes, styles, stops/TP/horizon, normal exits, reflections, MCP escalation |
| Risk/capital | Per-trade risk, max order, max positions, cash buffer, no all-in, no leverage |
| Reproducibility | Makefile, Docker, configs, lockable env, artifacts, tests, docs |
| Backtest/shadow | Event-driven backtest, HTML report, AI ablation, live-data shadow runs |

## Disclaimer

This software is for a hackathon and research. It is not investment advice. Crypto
markets and exchange fees can change without notice. Use shadow mode first and never
fund more than you can afford to lose.
