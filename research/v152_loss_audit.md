# V15.2 loss audit and execution repair

The available evidence does not establish a profitable directional strategy. V15.2 repairs execution, risk-budget and evidence-retention faults; it does not make the price model calibrated or guarantee returns. This is paper trading only.

## Ledger reconciliation

The uploaded `poly-edge-development-2026-10-18T19-49-15Z.zip` contains a snapshot at 2026-10-18 19:48:18 UTC. Its seven closed trades have net P&L **-$2.18560**: gross price P&L **-$1.35000**, fees **$0.83560**, one win and six losses. Fees worsen the result, but the direction/timing also loses before fees.

| Asset | Closed trades | Net P&L | Attribution |
|---|---:|---:|---|
| BTC | 2 | +$0.50975 | V15.1 |
| ETH | 2 | -$1.19500 | V15.1 |
| SOL | 2 | -$1.03845 | Inherited from V15 |
| XRP | 1 | -$0.46190 | Inherited from V15 |

V15.1 therefore contributed **-$0.68525** across four trades. The newer live checkpoint at 19:57:50 UTC contains an additional XRP loss of **$0.57360**, bringing the full ledger to **-$2.75920** across eight closes, including **$1.00920** in fees. Three lanes are risk-locked after consecutive losses. Release recovery preserves those losses and locks, and prefers a newer live handover to the bundled checkpoint; it does not reset the account to $200.

## Findings and changes

1. **The live book falls behind.** In the retained BTC journal, 49,451 book messages were rejected as stale. Their delay ranged from 17.24 to 53.46 seconds, median 43.57 seconds. ETH's median rejected-message delay was 11.25 seconds. Connection status alone does not establish freshness. Reconnect after severe or sustained stale backlog; request a fresh initial book; explicitly yield between buffered websocket frames. This addresses backlog recovery, without claiming its sole cause is known.
2. **Logging compounds load and loses the useful evidence.** The previous SQLite connection committed each raw insert individually. Repeated stale warnings also occupied a second row for almost every delayed book message. Batch raw writes, commit critical events immediately, and aggregate stale warnings at most once a second with cumulative rejection counts. Retain 100,000 raw rows, 20,000 state rows and 20,000 critical rows separately. Pin each export's SQLite read snapshot at capture time so pruning during compression cannot change it. Disk remains ephemeral; this is not durable cloud archiving.
3. **The strategy ignored configured stops.** Momentum entries always used a $0.30 stop even when asset risk settings differed. New positions use the configured asset stop. An entry is rejected when spread plus both taker fees leaves less than one tick or 25% of the stop, whichever is larger, for adverse movement. This is a fixed risk-budget rule, not an optimized prediction threshold. The net profit target is at least the configured net stop; this is an experimental payoff-policy change, not evidence that targets will be reached. Existing positions retain their original entry-time contract.
4. **Costs make stops fragile.** At a $0.50 ask, $0.49 bid and five shares, entry/exit fees plus spread consume approximately $0.225 before any adverse move, leaving $0.075 inside the old $0.30 stop. The official crypto taker formula is `shares * 0.07 * price * (1-price)`; makers have zero fees. Reference: https://docs.polymarket.com/trading/fees (checked 2026-10-18).
5. **Exit failure was partly invisible.** The BTC winner closed 57.5 seconds after entry, although management requested an exit after about 21 seconds and configured maximum hold was 30 seconds. Several exit attempts have no intervening rejection explanation. Post-latency stale-book exits previously returned silently. Add explicit rejection, wait and actual-latency evidence, a dedicated 100ms position-risk loop, and latch triggered exits until filled. A stop is a trigger, not a guaranteed maximum loss: fresh depth, executable prices and latency still constrain fills. No fictitious fills or unlimited-slippage exits are introduced.
6. **Late execution needs a deadline.** Reject directional entries when execution takes more than one second. Exits continue attempting execution against confirmed fresh bids. Publish scheduler lag, rejected stale-message counts and book event lag in diagnostics.
7. **Opening reference can arrive before market discovery.** Recover a timestamped TWAP sample already received within the existing eight-second opening window. Do not substitute the current price or widen the trust window. If the opening sample is absent, the lane still waits for a trustworthy next round.

## Validation and limits

All 67 automated tests pass, including funded fills and fees for all four assets, configured risk admission, stale exits and retries, execution deadlines, opening-reference recovery, backlog resynchronization, raw-log flooding, pinned exports, HTTP download behavior and release loss preservation. JavaScript syntax validation also passes.

`v152_chronological_execution_check.json` reports the earlier 60% and later 40% of each retained state window separately, with the same production rules and historical book ages. There are **zero eligible fills in both windows**. BTC and XRP were waiting for trusted openings; ETH and SOL were already locked in the recorded state. These short pruned windows cannot demonstrate profitability or compare realized exits. The replay initializes fresh paper accounts and reconstructs market inputs, rather than claiming to reproduce the historic risk locks. Neither a zero-trade replay nor passing software tests proves a trading edge.

The uploaded raw journals cover only the last approximately 117 seconds (BTC), 306 seconds (ETH), 580 seconds (SOL), and 388 seconds (XRP). Actual earlier fills/exits survive in the paper event ledger, but their tick windows do not. Exact counterfactual trade replay is therefore unavailable. Predictor thresholds were not optimized against these seven outcomes. A credible next strategy assessment needs complete chronological entry/exit observations, actual fee/slippage accounting, and later forward data kept separate from development data.
