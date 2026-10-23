# POLY / EDGE V16.2 — calibrated mispricing, skilled-wallet study and earned promotion

V16.2 adds `arb-lab-v4/poly_edge.py` (EdgeLab). Strategies now *earn* the right to trade paper money:

- **Calibrated fair value.** Every round, the heuristic oracle model's raw Up probability is recorded per time-left bucket. After Polymarket resolves the round (verified via Gamma), EdgeLab learns the empirical Up rate for that probability/time cell, shrunk toward the heuristic with 10 pseudo-rounds. Brier scores for both are tracked, each scored before its round is learned from.
- **Mispricing detector.** A price is called wrong only when the conservative calibrated probability (mean − 1 SE) exceeds the all-in cost (ask + taker fee) by a 2-cent buffer, with a fresh book, spread ≤ 3c and depth for the minimum order.
- **Skilled-wallet study.** Public trades for every resolved round are pulled from the Polymarket data API. Wallets with ≥ 6 studied rounds, positive total P&L and mean round P&L − 1 SE > 0 are ranked as skilled. Their entry profile and live net order flow in the current round are exposed. The `edge_smart` variant only takes mispricings that skilled flow agrees with.
- **Shadow first, then promotion.** Both variants place one virtual hold-to-resolution bet per asset per round. A variant is promoted to funded paper execution only while its last 200 bets show ≥ 40 bets over ≥ 20 rounds with mean return − 2 SE > 0, and is demoted automatically if that stops holding. The V16 research model likewise trades (as `MODEL_SCALP`) only when its existing out-of-sample evidence gate passes.
- **Sizing.** Quarter-Kelly on the conservative probability, capped at 15% of the asset's allocation and at the remaining lane/portfolio loss budget. The exchange minimum is used only when it does not exceed the full-Kelly stake. Larger orders and exits walk the book within one/two ticks.
- **Exits.** Mispricing positions (`EDGE_HOLD`) are held to verified resolution unless the market bid, after fees, exceeds the calibrated fair value by 1c. Model scalps keep the 30-second stop/target rules with stops scaled to size.
- **Autonomy.** Completed sessions roll over immediately; risk-locked lanes after 30 minutes; a portfolio lock after 60 minutes — never with open exposure. All losses stay in the lifetime ledger (`POLY_AUTO_CYCLE=0` disables this).
- **Readiness scorecard** (`GET /api/poly/readiness`): ≥ 300 funded paper trades, positive net P&L, mean − 2 SE > 0, max drawdown < 10% of capital, ≥ 14 days, and a currently promoted strategy. No live order adapter is installed.
- **Orb.** Region glow = conservative edge after fees, pulsing seed rings = a detected mispricing, bursts = paper fills, core = shadow/promoted status.

Persistence: EdgeLab state is saved to Postgres (`POLY_DATABASE_URL`) or `POLY_DATA_DIR/poly_edge_state.json`, and handed over between deploys through `POLY_HANDOVER_URL`, which is read before the new instance binds its port. `POLY_KEEPALIVE_URL` makes the service ping itself so a free instance does not sleep. Endpoints: `/api/poly/edge`, `/api/poly/edge/state`, `/api/poly/readiness`.

These are paper results on live markets. No strategy is asserted to be profitable.

# POLY / EDGE V15.1 — confirmed-trend paper execution

V15 placed and filled two live paper entries (SOL and XRP) and closed them at a combined -$0.81860. V15.1 rejects those cheap countertrend rebounds: estimated round direction must agree (fair probability at least 0.55), and the actual best bid must have advanced a full tick. An ask-only midpoint change cannot confirm a buy. These changes do not establish profitability. The prior closed paper ledger is checkpointed and the previous live instance is queried during this release startup so known losses are carried rather than presented as recovered. Existing local state takes precedence; hosting storage remains ephemeral.

59 tests pass. The tightened in-sample execution replay is in `research/v151_replay_summary.json`; it is not out-of-sample validation. The V15 replay and its limitations remain below for comparison.

# POLY / EDGE V15 — executable multi-asset paper workstation

V15 restores a funded marketable momentum execution path for BTC, ETH, SOL and XRP. It does not enable the older disabled lag trader. The previous staged-first-leg strategy could quote only 3–6 ticks below the book and recorded no fills. New passive first-leg quotes are suppressed while momentum is enabled; existing residual inventory is still managed, and genuinely executable complete sets retain priority.

Entries require a trusted opening reference, fresh oracle data (under 5 seconds), a current executable book (under 3 seconds), aligned oracle lead/impulse, a rising token midpoint over at least two seconds, no more than a 2-cent spread, confirmed depth on both sides, and estimated model advantage after both fees and a buffer. Model estimates are not calibrated probabilities. Orders use the exchange minimum (at least five shares), at most one tick of price movement, a 350ms simulated execution delay and full post-delay revalidation. There is no fill merely because a signal appears.

Per-entry notional including fees is capped at the smaller of $3.25 and 10% of the asset allocation. New positions use a $0.30 net loss trigger, $0.18 net profit trigger, 30-second maximum hold and one entry per asset per round. The existing $6 portfolio loss lock, per-lane session limits, 90-second loss cooldown and two-consecutive-loss locks remain. Triggers are not guaranteed execution prices or maximum realized losses. Marketable exits require confirmed current depth and price checks; unresolved exposure cannot be erased by a reset.

Book quantity updates now follow price-change messages, stale book events cannot overwrite current levels, and market discovery/REST refreshes no longer overwrite a healthy streaming book every second. Disk snapshots are throttled while execution events force a save. The UI shows actual open positions and momentum fills separately from closed-trade counts.

Validation: 55 execution/risk/export tests, Python compilation and JavaScript syntax checks. `arb-lab-v4/replay_momentum.py` exercises the production entry/exit code using retained STATE records. The included `research/v15_replay_summary.json` produced three closes: BTC -$0.38770, ETH +$0.70115, SOL/XRP zero fills; total +$0.31345 after modeled fees. This short, incomplete, in-sample execution regression is not a tick-level backtest or evidence of future profitability. Its sampling/latency/book-age limitations are explicit in the result.


BTC, ETH, SOL and XRP 5-minute crypto markets run concurrently. All four P&L cards, price/TWAP graphs and paper-P&L graphs share one dashboard, with a common all-asset network orb and execution tape. There are no asset-switch tabs.

## V14.3 strategy tuning, wallet pattern telemetry and execution filters

V14.3 adds interactive real-time strategy risk tuning (`/api/poly/strategy/config`), empirical wallet research findings (`/api/poly/research/summary`), filtered execution tape views (`FILLS`, `MERGES`, `QUOTES`, `EXITS`), 5-minute round expiration countdowns (`MM:SS`), and real-time locked-edge and depth balance metrics on all four asset lanes.

## Paper allocation and experiment settings

| Asset | Allocation | First-leg residual cost cap | Marked residual stop | Session loss gate | Fee-adjusted pair target |
|---|---:|---:|---:|---:|---:|
| BTC | $60 | $1.50 | $0.30 | $3.00 | 0.980 |
| ETH | $80 | $2.00 | $0.40 | $4.00 | 0.970 |
| SOL | $30 | $0.75 | $0.15 | $1.50 | 0.970 |
| XRP | $30 | $0.75 | $0.15 | $1.50 | 0.970 |

Total simulated capital: $200. Portfolio realized/marked loss gate: $6. These experimental allocations favor the BTC/ETH data coverage; they are not optimized investment weights. The database research covered BTC/ETH only, and its net profitability remains unverified. SOL/XRP are paper extensions. Risk gates do not guarantee maximum losses when liquidity or prices move abruptly.

V14 quotes passively at the minimum of the book bid, passive ask limit, exposure budget and affordable fee-adjusted complement price, at most six ticks behind the best bid. The complement must have sufficient current size and both legs must fit available cash. Quote processing revalidates the hedge budget. A trade-through requires enough accumulated observed volume, or the simulated ahead queue must be consumed, before an order fills. These are conservative paper assumptions, not verified real queue position or actual maker access. Directional lag entries stay disabled; the model remains a diagnostic input. The residual timeout remains 15 seconds; outstanding orphan quotes are canceled before exits and merges.

The uploaded V13.1 diagnostic had 393 BTC snapshots with zero quotes/fills. With V14 settings, 129 snapshots produced funded quote candidates, maximum projected all-in pair cost 0.97988. This is quote eligibility only, not an execution backtest or profit result. In the initial V13 trial, legacy directional BTC/SOL trades lost about $1.03; that path was disabled in V13.1. New paper allocations start a new experiment and do not recover old losses.

## V14.1 execution correctness

The first V14 uploaded run had 421 quotes, all cancelled, and zero fills or P&L. Existing quotes are now validated at their own fixed prices rather than cancelled when a new candidate prefers the other side. Trade prints are processed at receipt before subsequent book changes; book changes immediately revalidate resting quotes. Fills require matching market identity, quote lifetime, a current feed, confirmed SELL direction and enough queue/trade-through volume. Duplicate, old, missing-time, future, BUY and post-cancellation prints cannot manufacture fills. These fixes do not demonstrate profitability and do not loosen allocation or risk limits. Partial executions are not modeled.

## Run

Python 3.10+:

```bash
python -m pip install -r arb-lab-v4/requirements.txt
cd arb-lab-v4
python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

Open `/poly`. The dashboard calls the paper engine on its own origin; the unified server runs the engine itself (`POLY_ENGINE_ROLE=combined`, the default). Set `POLY_HANDOVER_URL` to a running engine's base URL to import its live ledger on startup.

Dedicated engine:

```bash
cd arb-lab-v4
python -m uvicorn poly_engine_service:app --host 0.0.0.0 --port 8766
```

`GET /api/poly/demo/server?all_assets=true` returns a coherent all-asset payload, portfolio totals and individual market, book, risk, paper and history snapshots. The default single-asset endpoint remains compatible. The **DOWNLOAD FULL DEV LOG** button fetches `GET /api/poly/demo/diagnostics`, a compressed ZIP containing the all-asset snapshot, four chronological NDJSON audit journals and a coverage manifest. Raw CLOB messages and relevant RTDS messages are recorded server-side even when the browser is closed; REST responses, quote IDs, quote/print evaluations, fill evidence, cancellations, one-second state/risk/entry-gate context, worker crashes and feed errors support later development. No signing keys, auth headers or environment secrets are logged. ZIP compression runs outside the feed event loop, using captured per-asset sequence cutoffs. Journals retain a rolling tail of 100,000 events per asset (up to 999 additional events before pruning); exports report retained sequence ranges, pruning and logging errors. They live on ephemeral server disk: download before a restart or deployment. Journals are diagnostic evidence, not exchange-confirmed executions or complete historical ledgers. Feed status reflects observed freshness; the orb is illustrative.

## Verification

```bash
python -m unittest discover -s arb-lab-v4 -p "test_*.py" -v
node --check arb-lab-v4/poly.js
node --check arb-lab-v4/poly-orb.js
```

41 risk/inventory/multi-asset/quote-lifecycle/export tests. Expired positions require an authoritative closed/resolved result; the latest tick is not treated as settlement. Separate asset state files use `/tmp/poly_v14_<asset>_paper.json`, which can disappear on hosting restart. No credentials or signing keys are needed for this paper engine; no real orders are submitted.

## Retained project components

The root `/` retains ARB/LAB. Root Node sources remain legacy; `npm start` does not start the Python dashboard. `wallet_research_service.py` and `wallet_strategy_research.py` retain the research service; its temporary database resets on startup. `tools/wallet-downloader/` provides the resumable Windows archive downloader. `previews/Network_Orb_Preview.html` is an older standalone orb preview, not the current live dashboard. Legacy single-asset scripts remain in the repository but V14 does not load them.

