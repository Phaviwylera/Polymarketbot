# POLY / EDGE V14.1 — all-asset paper workstation

BTC, ETH, SOL and XRP 5-minute crypto markets run concurrently. All four P&L cards, price/TWAP graphs and paper-P&L graphs share one dashboard, with a common all-asset network orb and execution tape. There are no asset-switch tabs.

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

Open `/poly`. The dashboard uses the deployed dedicated engine URL in `poly.js`; set `BASE` there to an empty string for a combined local server. `POLY_ENGINE_ROLE=frontend` skips the local engine when deploying the dashboard separately.

Dedicated engine:

```bash
cd arb-lab-v4
python -m uvicorn poly_engine_service:app --host 0.0.0.0 --port 8766
```

`GET /api/poly/demo/server?all_assets=true` returns a coherent all-asset payload, portfolio totals and individual market, book, risk, paper and history snapshots. The default single-asset endpoint remains compatible. The **DOWNLOAD FULL DEV LOG** button fetches `GET /api/poly/demo/diagnostics`, a compressed ZIP containing the all-asset snapshot, four chronological NDJSON audit journals and a coverage manifest. Raw CLOB messages and relevant RTDS messages are recorded server-side even when the browser is closed; REST responses, quote IDs, quote/print evaluations, fill evidence, cancellations, one-second state/risk/entry-gate context, worker crashes and feed errors support later development. No signing keys, auth headers or environment secrets are logged. Journals retain a rolling tail of 100,000 events per asset (up to 999 additional events before pruning); exports report retained sequence ranges, pruning and logging errors. They live on ephemeral server disk: download before a restart or deployment. Journals are diagnostic evidence, not exchange-confirmed executions or complete historical ledgers. Feed status reflects observed freshness; the orb is illustrative.

## Verification

```bash
python -m unittest discover -s arb-lab-v4 -p "test_*.py" -v
node --check arb-lab-v4/poly.js
node --check arb-lab-v4/poly-orb.js
```

40 risk/inventory/multi-asset/quote-lifecycle/export tests. Expired positions require an authoritative closed/resolved result; the latest tick is not treated as settlement. Separate asset state files use `/tmp/poly_v14_<asset>_paper.json`, which can disappear on hosting restart. No credentials or signing keys are needed for this paper engine; no real orders are submitted.

## Retained project components

The root `/` retains ARB/LAB. Root Node sources remain legacy; `npm start` does not start the Python dashboard. `wallet_research_service.py` and `wallet_strategy_research.py` retain the research service; its temporary database resets on startup. `tools/wallet-downloader/` provides the resumable Windows archive downloader. `previews/Network_Orb_Preview.html` is an older standalone orb preview, not the current live dashboard. Legacy single-asset scripts remain in the repository but V14 does not load them.
