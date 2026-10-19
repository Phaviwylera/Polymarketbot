# V16.1: isolated two-sided inventory research

This release adds a forward-only maker-first / taker-hedge shadow lane beside the existing funded paper baseline. It does not promote a trained model, change funded entry rules, change allocations, reset sessions or clear loss locks. The orb and existing charts are preserved.

## Evidence and scope

The uploaded development export showed repeated stale-book resynchronizations, no new funded closes, retained BTC/ETH loss locks and an ended three-hour paper session. It also contained many conservative censored directional labels. A missing executable observation is not proof of a realized trading loss. Existing conservative labels and training policy are preserved; the UI and `research-quality.json` now expose that distinction. Feed freshness checks remain strict.

The wallet analysis supports testing two-sided inventory, not copying a proven arbitrage strategy. Buying both tokens at lifetime average prices below $1 does not establish simultaneous executable margin. Maker/taker roles and historical cash-flow profit remain unverified. The old unsupported 1.2-second signal half-life claim is removed from both summary endpoints and the Research panel.

## Causal fill assumptions

`poly_pair_shadow.py` owns virtual cash and a non-overlapping state machine for each asset. Quotes use the lesser of the best bid, the tick-rounded price meeting a $0.98 combined-cost ceiling, and the residual cost cap. Quotes below retained bid depth are rejected because their queue is unknown. Entry requires fresh, internally consistent books and enough observed depth for a hedge and unwind.

A quote activates only after a separate 350ms observation delay. Queue ahead is at least the larger of submission and activation displayed queue. Old prints are discarded on activation. Thereafter, only same-market, same-token, fresh sell-aggressor prints received after activation, with an explicit feed-ready flag and prices at or below the quote, consume the queue. Full queue plus order quantity must trade. Touches, unknown aggressors and book-size decreases cannot fill. This conservative proxy is not proof of a real exchange fill; hidden liquidity, queue priority and partial fills remain unknown.

A first-leg fill debits virtual cash immediately. A hedge is evaluated only after another 350ms. It requires complete current ask depth, available virtual cash and the $0.98 combined-cost ceiling after an assumed per-share `0.07*p*(1-p)` taker fee. No rebate is credited. A completed pair receives hypothetical complete-set collateral credit, net of hedge spend. It is not a real merge or settlement.

If the hedge becomes unattractive after the first fill, the inventory remains exposed. Stops and a 15-second unhedged timeout request a separate delayed unwind. Missing data, rollover or a three-second unavailable exit records a full first-leg cost bound. Bounds and observed proxy P&L are distinct fields; both enter conservative virtual cash. Never interpret the proxy ledger as funded paper performance.

## Bounds and persistence

Each asset gets its own virtual starting capital matching its allocation. Cycle cost is capped at `min($8, 20% allocation)`, first-leg cost at `min($3.25, 5% allocation)`, stop at `min($0.40, 0.5% allocation)`, and cumulative conservative loss gate at 3% allocation. Stops can overshoot with latency or gaps. At most one quote attempt per asset per five-minute round; ten-second quote TTL; no entry in the first 20 or last 75 seconds. All assets collect independent evidence; no automatic capital reallocation or order promotion exists.

PostgreSQL saves pair state in the same transaction as paper checkpoints and durable event records. New event kinds include quote, cancellation, first fill, hedge, exit request and outcome. Pair event retention is bounded at 20,000 per kind. The latest 200 outcomes per asset accompany checkpoint totals. On recovery, unfilled quotes are cancelled because queue continuity is unknown; open first-leg exposure becomes a separately identified conservative bound exactly once per recovered state. Existing pre-pair checkpoints remain compatible. No schema migration or unrelated table mutation is required.

## Interfaces and review

`GET /api/poly/research/pairs` and `research.pairShadow` expose aggregate and per-asset state, counters, limits, fees, unhedged cost and observed/bounded P&L. The dashboard keeps all four lanes in one view and labels ended funded sessions. Dev ZIPs add `pair-shadow.json` and `research-quality.json`; existing files remain present. RESEARCH.ndjson retains causal evidence. Pair errors are isolated and exposed rather than silently promoting or modifying baseline execution.

Validation covers activation/queue consumption, duplicate/old/unready print rejection, book freshness and depth budgets, hedge latency, adverse hedge movement after fill, stop execution, fee/cash reconciliation, loss limits, restart bounds, paper isolation and durable event/checkpoint transactions. Existing lifecycle, recovery, momentum, inventory, diagnostic download and orb geometry regressions also run.

Evaluate fresh per-asset/window cohorts under conservative costs, failed hedges, gaps and missed fills before considering any funded promotion. The number of overlapping historical directional probes is not an independent sample count. Positive shadow totals alone do not establish durable profitability.
