# POLY / EDGE — Polymarket bot project

Full source snapshot of the dashboard, V13 multi-asset paper engine, terminal-themed network orb and wallet research tools.

## Run the dashboard locally

Requires Python 3.10 or newer. From the repository root:

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
# source .venv/bin/activate
python -m pip install -r arb-lab-v4/requirements.txt
cd arb-lab-v4
python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

Open http://127.0.0.1:8765/poly for POLY / EDGE. The root `/` retains the earlier ARB/LAB dashboard.

The dashboard currently polls the deployed dedicated engine at `https://poly-edge-paper-engine.onrender.com` (see `POLY_ENGINE_BASE` at the top of `arb-lab-v4/poly.js`). To use the combined local engine, set that constant to an empty string and reload. It then polls the same-origin `/api/poly/demo/server` endpoint. The server's existing role/environment settings are retained.

## Included source

- `arb-lab-v4/poly.html`, `poly.js`, `poly.css`, `poly-demo.js`, `poly-log.js`: trading dashboard and downloadable diagnostics.
- `arb-lab-v4/poly-orb.js`: amber/cyan spherical network with incoming telemetry signal animation, sparse orbiting particles, 40-second rotation loop, reduced-motion support, pause/play and 3840×2160 PNG export.
- `arb-lab-v4/poly_server_demo.py`: asset-isolated V13 paper-trading engine and inventory risk controls.
- `arb-lab-v4/poly_portfolio.py`: BTC/ETH/SOL/XRP 5-minute parallel lanes, $5 each ($20 total), aggregate $1.50 marked/realized loss gate.
- `arb-lab-v4/test_multi_asset.py`: 9 asset isolation, capital, resolution and risk tests.
- `arb-lab-v4/poly_engine_service.py`: separate FastAPI engine service.
- `arb-lab-v4/test_inventory_risk.py`: 14 regression tests for risk and inventory order handling.
- `arb-lab-v4/wallet_research_service.py` and `wallet_strategy_research.py`: current hosted research service source.
- `tools/wallet-downloader/`: resumable Windows public-wallet downloader, defaulting to `D:\Phavi\PolyWalletResearch`.
- `previews/Network_Orb_Preview.html`: self-contained offline orb preview. Open directly in a browser.
- `arb-lab-v4/index.html`, `app.js`, `market.js`, `style.css`: retained ARB/LAB dashboard.
- Root `server.js`, `package.json`, `package-lock.json`: retained legacy Node service; `npm start` runs that legacy service, not the Python dashboard.

## Run the dedicated services

From `arb-lab-v4`, with the Python dependencies installed:

```bash
python -m uvicorn poly_engine_service:app --host 0.0.0.0 --port 8766
python -m uvicorn wallet_research_service:app --host 0.0.0.0 --port 8767
```

Run these commands in separate terminals when needed. The research service starts its collection task on startup. Its current implementation uses a temporary database and starts over on process startup; use `tools/wallet-downloader` for a resumable local archive. Research coverage and PnL reconciliation remain incomplete until verified reports are generated.

The dedicated service CORS origins and self-health URL still refer to the existing Render deployment. Adjust those explicitly when setting up different hosts.

## Validation

```bash
python -m unittest discover -s arb-lab-v4 -p "test_*.py" -v
node --check arb-lab-v4/poly-orb.js
node --check arb-lab-v4/poly.js
```

The orb passed geometry, loop-continuity and finite-coordinate checks at mobile, desktop and 4K dimensions. Browser visual QA was unavailable in the authoring environment.

## Trading and deployment status

POLY / EDGE executes simulated paper trades. Its controls do not establish profitability or eliminate unmatched-fill risk. The retained ARB/LAB browser wallet and swap interface is a separate experimental feature described in `arb-lab-v4/README.md`.

This repository upload includes the orb upgrade but does not deploy it. Existing Render services still use their previously configured source repository. No wallet dataset, local database, private key or account credential is included in this source snapshot.

## V13 paper scope

All four crypto 5-minute scanners run concurrently. The dashboard shows total portfolio equity and selectable per-asset details. Each lane has a separate $5 simulated allocation and a $0.50 loss gate; all lanes stop opening exposure at a combined $1.50 realized or marked loss. The first-leg residual cap remains $0.50 and the timeout remains 15 seconds. Market minimum sizes and fee-adjusted hedge funding can block trades at this small balance. These limits are gates, not guaranteed maximum losses during illiquidity or gaps.

Wallet-inspired staged inventory starts with a conservative queue-simulated first-side quote, then an affordable fee-aware complement or bounded recovery exit. Pair targets are 0.980 BTC and 0.970 ETH/SOL/XRP; these are experiment settings, not inferred wallet parameters. Executable complete-set paths remain active. Directional lag entries are disabled in V13.1 after the first V13 paper trial lost about $1.03 across BTC and SOL; signal diagnostics remain visible. The new deployment starts a fresh paper run, not a recovery of those losses. No signing keys, account credentials or real orders are used.

The evidence covers BTC/ETH only. SOL/XRP are unvalidated paper extensions and require their own market and matching fresh reference feed. Unavailable assets stay gated. Expired residuals wait for an authoritative closed/resolved market result; unresolved inventory is preserved and prevents market rollover. A feed's latest tick is not treated as settlement.

GET `/api/poly/demo/server?asset=ETH` returns ETH details plus all portfolio lane summaries. BTC is the default; unsupported assets are rejected. State files are per asset under `/tmp`; hosting restarts can discard ephemeral paper history. V13 starts a new experiment and does not recover earlier paper losses.
