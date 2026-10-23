# Live incident — 2026-10-23 06:26–07:17 UTC

Run: `live_20261023_062625_nvuw` · mode: **live** · strategy: 1-hour swing
(`SWING_TIMEFRAME_HOURS=1`) · 12 symbols.

## What filled

| Symbol | Gross quantity | Fill price | Fee in base | Net balance |
|---|---:|---:|---:|---:|
| PEPE | 149,712 | 1.2511 TMN | 449.136 PEPE | 149,262.864 PEPE |
| BNB | 0.0003 | 202,000,000 TMN | 0.0000009 BNB | 0.0002991 BNB |

TMN cash after the two buys: **4,752,095 TMN**. No open orders. No sell filled.

## What went wrong

1. Wallex charged the buy fees in the **base asset**. The old accounting booked the
   full gross quantity and converted the fee to TMN, so cash and base balances diverged.
2. Reconciliation correctly detected the divergence after three checks and killed the
   engine. It then attempted exits for the **gross** quantity (149,712 PEPE), which
   Wallex rejected (`error_code 1006`, “balance 149,262.86 PEPE”).
3. `LiveBroker` overwrote the OMS request metadata with the exchange payload, so the
   positions silently fell back to the 20-minute default horizon instead of the
   20-day TREND horizon. The exit loop retried for the rest of the session: 1,393
   rejected sell orders, no fills.
4. The operator stopped after 51 minutes. The failed exits did not realize a loss, but
   the two positions were left open and unmanaged.

## Resolution

At 07:32 UTC the exchange order history shows two sells from a different client run
(`argus_3117jo...`):

- PEPE 149,262 at 1.2479
- BNB 0.00029 at 202,029,823

The flatten command run at 07:34 then found only dust and correctly sent **zero**
orders (`closed: 0`). Current balances:

- TMN cash: **4,996,524**
- PEPE: **0.864** (about 1 TMN; below the exchange step)
- BNB: **0.0000091** (about 2 TMN; below the exchange step/min notional)
- Open orders: **0**
- Actual realized result: about **−3,476 TMN** versus the 5M start, including fees and
  spread on the two round trips.

The dust is not sellable through the normal market step; it is ignored by startup
reconciliation. No further flatten action is needed.

## Fixes in the repository

- `Fill.base_fee_quantity`; base fee reduces credited quantity, not TMN cash.
- All-in average entry; realized PnL no longer double-counts the entry fee.
- `LiveBroker` preserves `entry_meta` under `exchange_payload`.
- Exit-rejection backoff (60s) and no duplicate sell while one is open.
- `entries_enabled=False` when the kill switch activates.
- Startup reconciles restored live quantities to actual exchange balances.
- 42 unit tests pass; ruff/mypy clean; Docker build passes.

## Required action

None for the dust. Run shadow with the fixed accounting before starting a new live
canary. If another real position is ever left open, use:

```bash
MODE=live LIVE_CONFIRM=I_UNDERSTAND_REAL_MONEY \
  .venv/bin/python -m wallex_trader flatten --mode live --confirm-live
```
