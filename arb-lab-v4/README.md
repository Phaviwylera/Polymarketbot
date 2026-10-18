# ARB/LAB — one integrated website

This project is ONE browser-accessible website containing the quote scanner, dedicated browser-managed bot wallet, automation controls, transaction logs, and withdrawal workflow. No separate Python engine app, Windows BAT launcher, or separately installed wallet service. Source consists of frontend and an API gateway, deployed together as one web service.

**High-risk alpha software, NOT mainnet-verified and NOT security audited. Do not fund an untested wallet.** Real automation is strictly user-armed; it requires the page to remain open and the browser-encrypted bot wallet unlocked. Never use your primary Phantom recovery phrase. Orders are two non-atomic swaps and realized profitability cannot be guaranteed. A positive quote is not a risk-free arbitrage trade.

Wallet key material is generated in the browser and encrypted with a user passphrase (AES-GCM PBKDF2). The public web server receives ONLY public wallet addresses, signed transactions, Jupiter quote credentials, and nonsecret RPC requests. Users are required to save an encrypted recovery backup and ensure they remember its password BEFORE funding; losing both browser storage and backup loses assets forever. No funds, passwords, or private key bytes should be uploaded to the server.

Hosted service requires access to `api.jup.ag` and a mainnet RPC; quote API key is entered into website session or supplied as server `JUPITER_API_KEY` environment variable. Users can get a free key at https://portal.jup.ag. Key is not a wallet key. The browser loads web3.js v1 from CDN, so users must trust their browser, the site, and this dependency. For production amounts use audited, secure smart-contract-based execution and dedicated custody infrastructure instead.

Run locally for testing: `pip install -r requirements.txt && uvicorn server:app --host 127.0.0.1 --port 8765`. Go to http://127.0.0.1:8765.

The site does not remain trading after closing or locking the browser tab; Render free-tier services sleep and should not be used for uptime-critical trading. The default mode is disarmed. For a $2 wallet, fees, account rent and route minimums will generally defeat possible gains. Be prepared for zero trades or total loss.