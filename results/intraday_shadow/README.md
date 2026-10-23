# Intraday live-data shadow samples

These ran against live Wallex data with 12 symbols and **never sent an order**.

| Bar | Run | Duration | Candidates | Decisions | Fills |
|---|---|---:|---:|---:|---:|
| 1m | `shadow_20261023_060325_ehh4` | 3 min | 0 | 0 | 0 |
| 2m | `shadow_20261023_060720_acge` | 2 min | 0 | 0 | 0 |
| 5m | `shadow_20261023_060952_tvnn` | 2 min | 0 | 0 | 0 |

The 113-day 1m backtest averages about 1.2 decisions/day, so a 2–3 minute window is not
a meaningful frequency test. The engine correctly did not force trades. Backtest
results for the same bar sizes are in `results/backtest_1m_intraday`,
`results/backtest_2m_intraday` and `results/backtest_5m_intraday`.
