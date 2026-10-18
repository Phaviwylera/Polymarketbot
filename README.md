# POLY / EDGE — Polymarket bot project

Full source snapshot of the dashboard, V12.1 paper engine, orange/blue network orb and wallet research tools.

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
- `arb-lab-v4/poly-orb.js`: centered orange/blue spherical network, sparse orbiting particles, 40-second rotation loop, reduced-motion support, pause/play and 3840×2160 PNG export.
- `arb-lab-v4/poly_server_demo.py`: V12.1 paper-trading engine and inventory risk controls.
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
python -m unittest discover -s arb-lab-v4 -p "test_inventory_risk.py" -v
node --check arb-lab-v4/poly-orb.js
node --check arb-lab-v4/poly.js
```

The orb passed geometry, loop-continuity and finite-coordinate checks at mobile, desktop and 4K dimensions. Browser visual QA was unavailable in the authoring environment.

## Trading and deployment status

POLY / EDGE executes simulated paper trades. Its controls do not establish profitability or eliminate unmatched-fill risk. The retained ARB/LAB browser wallet and swap interface is a separate experimental feature described in `arb-lab-v4/README.md`.

This repository upload includes the orb upgrade but does not deploy it. Existing Render services still use their previously configured source repository. No wallet dataset, local database, private key or account credential is included in this source snapshot.
