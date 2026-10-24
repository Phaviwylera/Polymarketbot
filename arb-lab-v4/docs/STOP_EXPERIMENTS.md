# Stop-slippage investigation and experiments

## Baseline (frozen 2026-10-25)

* Code revision `2cf2731` (local tag `baseline-2026-10-25`; the remote refused tag pushes from the
  build session, so the commit id is the reference). Strategy `momentum-17.8`, config hash
  `cfa5d71516e3`, experiments hash `96ff8357b1ea` (`poly_strategy.py`).
* Rules: entry 8–45 ¢, estimated edge ≥ 12 ¢, oracle lead ≥ 0.2 bps, impulse ≥ 1.0 bps (re-checked at
  execution), book move ½ tick–5 ¢, spread ≤ 2 ¢, fair ≥ 0.55, ~5 shares. Exits: net stop (lane
  `max(0.30, min(0.40, allocation×0.005))`: $0.30 BTC 5m, $0.40 ETH 5m), target `max(0.18, stop)`,
  30 s max hold, oracle reversal, 15 s before close. 350 ms simulated latency; stale book > 3 s rejected.
* Audit at 2026-10-24 22:01 UTC: FAIL — 50 out-of-sample trades (need 60), 3/6 test blocks
  non-negative (need 60 %); OOS mean +$0.26, block-bootstrap 95 % +$0.009…+$0.51 (44 rounds);
  live rules 30 trades −$0.08; fill audit clean (54 trades); ledger reconciled (43 sessions).
* The full baseline (identity, trade history, attribution, audit) is written once to the database
  row `baseline_2026_10_25` and served at `GET /api/poly/baseline`.

**Freeze:** while the experiments collect their sample, entry thresholds, sizing, take-profit, stop,
filters and market selection are not tuned. Any change alters the config hash, and the audit reports
results per hash, so samples from different configurations are never pooled.

## Stop path (code trace)

1. `poly_server_demo._momentum_manage` (position-risk loop, every 0.1 s) computes
   `net = shares × (bid − fee(bid)) − entryTotalCost` at the best bid. `net ≤ −stopLossUsd` → reason
   `momentum net loss stop`. The stop is a **net** budget: spread + both taker fees already use
   ≈ $0.22 of a $0.30 stop on 5 shares, so it fires after ≈ 1–2 ticks of adverse movement.
2. `_try_sell` → `_sell_walk`: requires the whole quantity within 2 ticks of the current bid
   (`poly_features.walk`, all-or-nothing, no partial fills), records `ORDER` (price = bid at decision),
   waits 350 ms, rejects a book older than 3 s, walks again; failure → `NOFILL` and a retry from the
   new, lower bid on the next tick.
3. `_close_scalp` → ledger (`cash`, `fees`, `realizedPnl`, `trades`), `SELL` event (VWAP), and
   `poly_attribution.closed_trade` (`stopOvershootUsd = max(0, −net − stop)`).

A stop is a trigger, not a guaranteed maximum loss: the realized loss is the trigger loss plus the
movement during latency and retries.

## Root-cause evidence

`poly_forensics.reconstruct_stops` rebuilds every stop from the session event chain (BUY → exit ORDER
→ NOFILL… → SELL): threshold bid, bid at the decision, retries, decision-to-fill time, and the split of
the overshoot into (a) loss already beyond the stop when it fired and (b) movement after it fired.
Results are in `GET /api/poly/audit` → `stopForensics` and the dashboard STOPS row.

From the 30 stop trades in the logged attribution (2026-10-23 10:24 → 2026-10-24 21:17 UTC):

* **Structural:** with ≈ $0.08 of adverse room inside the stop, a 1–2 tick overshoot ($0.02–0.10)
  is expected on every stop.
* **Chasing:** the largest overshoots entered after the token had already moved 13–24 ¢ (book move
  0.135–0.24): −$1.51, −$1.01, −$0.95, −$0.89, −$0.63. Entries like these are blocked by the V17.6
  no-chase rule (book move < 5 ¢).
* **Fast gaps on cheap tokens:** after V17.6, overshoots of $0.15–0.33 at normal latency (≈ 353 ms):
  0.30→0.20, 0.39→0.30, 0.21→0.12 — 9–10 ticks in about one latency window; low-priced tokens move
  more ticks for the same probability change.
* **Data gap:** the trigger-time bid depth was not recorded before this release, and the archive rebuilt
  from logs has only BUY/SELL events, so for those stops the split between (a) and (b) is not provable.
  Every trade closed from now on records `exitTriggerBid`, the top 5 bid levels at the trigger, the book
  age, retries and decision-to-fill time.

## Experiment A — earlier adverse-move exit (shadow)

* Hypothesis: exiting at 60 % of the stop, or when the bid is 2 ticks below the entry bid while the
  oracle lead points against the trade, reduces stop overshoot and worst losses without giving up more
  in missed recoveries than it saves.
* Parameters (`EXPERIMENTS['A']`, id `A-earlier-exit-v1`): `stopFraction 0.6`, `adverseTicks 2`,
  latency 0.35 s, walk 2 ticks. Uses only the live book and oracle at each tick.
* Recorded per trade: shadow exit price/net, trigger reason, attempts, `exitedEarlier`, `missedRecovery`.

## Experiment B — depth-aware entry gate (shadow)

* Hypothesis: entries whose bids cannot absorb the position after an adverse move produce most of the
  large overshoots; rejecting them improves net expectancy.
* Parameters (`EXPERIMENTS['B']`, id `B-depth-gate-v1`): reject when bid depth within 2 ticks
  < 2 × shares, or when selling into the current bids shifted 3 ticks lower (fees included) loses more
  than 1.0 × stop. Evaluated on the order-time book.
* Recorded per trade: `wouldReject`, reason, depth, stressed loss.

Both are shadow only: no order, cash or ledger effect (tested in `test_stop_experiments.py`).

## Evaluation and decision

* Window: trades with config hash `cfa5d71516e3` closed after this deploy; parameters frozen.
* Metrics (audit `experiments`): overshoot frequency/severity, mean/median/p95/worst loss, total and
  average net, exited-earlier and missed-recovery counts, reject rate, block-bootstrap difference per
  trade (5-minute rounds as blocks), by asset/time of day/edge band (`bands`).
* A policy is considered only if, on at least 60 evaluation trades, its block-bootstrap 95 % lower bound
  of the **difference** versus baseline is above zero and it also passes the baseline's own gates
  (positive mean, 60 % of blocks non-negative, drawdown < $10, survives 1-tick-worse exits). Lowering
  the worst loss in a small sample is not sufficient. Every variant tested is listed here, including
  failures, so multiple-testing risk stays visible.

| Variant | Status |
|---|---|
| A-earlier-exit-v1 | collecting (shadow) |
| B-depth-gate-v1 | collecting (shadow) |
