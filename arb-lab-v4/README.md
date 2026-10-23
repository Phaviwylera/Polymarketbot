# ARB/LAB — one integrated website

This project is ONE browser-accessible website containing the quote scanner, dedicated browser-managed bot wallet, automation controls, transaction logs, and withdrawal workflow. No separate Python engine app, Windows BAT launcher, or separately installed wallet service. Source consists of frontend and an API gateway, deployed together as one web service.

**High-risk alpha software, NOT mainnet-verified and NOT security audited. Do not fund an untested wallet.** Real automation is strictly user-armed; it requires the page to remain open and the browser-encrypted bot wallet unlocked. Never use your primary Phantom recovery phrase. Orders are two non-atomic swaps and realized profitability cannot be guaranteed. A positive quote is not a risk-free arbitrage trade.

Wallet key material is generated in the browser and encrypted with a user passphrase (AES-GCM PBKDF2). The public web server receives ONLY public wallet addresses, signed transactions, Jupiter quote credentials, and nonsecret RPC requests. Users are required to save an encrypted recovery backup and ensure they remember its password BEFORE funding; losing both browser storage and backup loses assets forever. No funds, passwords, or private key bytes should be uploaded to the server.

Hosted service requires access to `api.jup.ag` and a mainnet RPC; quote API key is entered into website session or supplied as server `JUPITER_API_KEY` environment variable. Users can get a free key at https://portal.jup.ag. Key is not a wallet key. The browser loads web3.js v1 from CDN, so users must trust their browser, the site, and this dependency. For production amounts use audited, secure smart-contract-based execution and dedicated custody infrastructure instead.

Run locally for testing: `pip install -r requirements.txt && uvicorn server:app --host 127.0.0.1 --port 8765`. Go to http://127.0.0.1:8765.

The site does not remain trading after closing or locking the browser tab; Render free-tier services sleep and should not be used for uptime-critical trading. The default mode is disarmed. For a $2 wallet, fees, account rent and route minimums will generally defeat possible gains. Be prepared for zero trades or total loss.
## V17.0 — market-trust calibration (edge_cal)

Analysis of 74 settled EdgeLab shadow bets: the heuristic model claimed a 52% average win chance but won 31%;
its Brier score (0.244) was worse than the market price alone (0.200). Regressing (outcome − price) on
(model − price) gives ~0.05 ± 0.22, so the model's "mispricings" were mostly false. The new `edge_cal`
variant uses `p = price + trust × (model − price)`, with `trust` learned online from settled raw bets
(ridge-shrunk toward 0 and cut by one standard error). It starts with an empty record and must pass the
normal promotion gate on future bets. `modelTrust` is shown in `/api/poly/dev/report.txt`.
Momentum (the only strategy with positive out-of-sample evidence) is unchanged.

## V17.1 — 45¢ entry ceiling and maker profit-target exits

* Momentum entries are capped at 45¢ (was 55¢). On 78 closed trades, sub-45¢ entries made money in both
  chronological halves (+$0.50 and +$0.52 per trade) while 45–55¢ lost in both (−$0.08, −$0.09).
* The profit target now rests as a paper maker sell (zero fee) at the lowest tick that meets the target.
  Paper fills only when the best bid trades through that price (bid ≥ price + 1 tick, book ≤ 3 s old).
  Stops, direction-reversal and time exits still sell as takers. Set `POLY_MAKER_EXIT=0` to switch off.

## V17.2 — profit target back to a taker exit

The V17.1 resting maker sell is now off by default (`POLY_MAKER_EXIT=1` turns it back on). A resting
order fills only at its own price, so it capped wins at the target (~$0.31), while taker target exits
had averaged +$0.87 by selling into price jumps; the fee saved (~$0.08 per exit) did not cover that.
