# Experiments register

One row per pre-registered test. The verdict is filled in only when the test reaches its sample size.
"Variants tried" counts every setting looked at before freezing the rule (more variants = more likely a fluke).

| Lane | Hypothesis | Registered (UTC) | Pass rule | Variants tried | Status 2026-11-09 | Verdict |
|---|---|---|---|---|---|---|
| S2 spot lead | Live spot runs ahead of the 5m BTC/ETH price | 2026-10-30 21:00 | 300 trades, lower 95% > 0 with 0.5c delay | 8 (backtest later found to have a look-ahead bug) | 2,183 trades, -0.63c per $, bad case -1.87c | **Failed** |
| M3 slow-market maker | Quoting liquid sports/politics markets earns spread + rebate | 2026-11-10 00:00 (re-registered) | 50 market-days, lower 95% > 0 | 1 | Restarted after the Data API v2 and survivorship fixes | Collecting |
| K1 Polymarket vs Kalshi | The two venues' 15m BTC/ETH prices sometimes sum to < $1 | 2026-10-26 | lower 95% > 0 | 1 | 1,118 rounds, bad case +0.08c (just turned positive) | Confirmed on paper; venues disagree on ~1.7% of outcomes; Kalshi not usable from India |
| R1 pair replica | GoodFallen's buy-both-sides pattern | 2026-10-26 | lower 95% > 0 | 2 | about -25c per $ | **Failed** |
| M1 slow maker (crypto) | Joining the best bid on both sides | 2026-10-25 | lower 95% > 0 | 5 | about -10c per $ | **Failed** |
| M2 fast maker | Requoting at 250 ms with pairing and stops | 2026-10-25 | lower 95% > 0 | 1 | about -17c per $ | **Failed** |
| X1 options gap | Deribit-implied odds vs Polymarket | 2026-10-25 | lower 95% > 0 | 1 | about -33% per event | **Failed** |
| H1 early overreaction | Fade early moves in 5m rounds | 2026-10-24 | lower 95% > 0 | grid | about -8c per $ | **Failed** |
| H2 Jev LLM | LLM forecaster vs price | 2026-10-26 | lower 95% > 0 | 1 | about -32c per $ | **Failed** |
| E1 event favourites | 80-95c favourites 3 days out | 2026-10-26 | lower 95% > 0 | 1 | no edge in history; forward collecting | Collecting |
| N1 / S1 event arbitrage | NegRisk baskets; resolution window | 2026-10-26 | lower 95% > 0 | 1 | N1: no gaps seen. S1: quarantined (settlement bug), restarted | Collecting |

## Research results not yet registered

- 2026-11-01 S2 improvement search: 960 variants, none survived the untouched test (`research/s2_search.py`).
- 2026-11-09 v3 model (gradient boosting with spot momentum, book imbalance, flow): first run was invalid (training
  kept only early-round prints); rerun in progress (`research/v3_model.py`).

## Standing risk review (2026-11-01)

Real-money trading is blocked: no lane passes after realistic costs, India's online-gaming law (PROGA, in force
2026-05-01) is treated by the government as covering prediction markets, the deploy path auto-runs main, and paper
fills are optimistic. See the review in the session report of 2026-11-01.
