# Evolution log

Newest first. One entry per daily run: findings, fixes (with commits), verification, open questions for the owner.

## 2026-11-09 (set-up, by hand)

**Findings**
- Polymarket retired Data API v1 on 2026-10-24; `/trades` answers 410 Gone from about 4 Nov. M3 saw no fills for 5
  days (237,156 errors); the wallet-skill study, history backfill and rival study were also blind.
- M3's test counted only markets still held, so closed markets and their settlement jumps dropped out (survivorship).
  Its 2026-10-31 "CONFIRMED" was on that biased count. Real total P&L about +$20.78 over two weeks.
- S2 failed its test: 2,183 trades, -0.63c per $, bad case -1.87c. Its equity was a random walk (peak +$45.69,
  trough -$12.53).
- K1 (Polymarket vs Kalshi) turned positive: 1,118 rounds, bad case +0.08c.

**Fixed** (all tests passing before each push)
- `4687017` Data API v2 adapter (`poly_dataapi.py`) for M3, the engine study, history and rivals; M3 survivorship
  fix; M3 re-registered from 2026-11-10 00:00 UTC.
- `a57f7ee` one-time full tape export (6.5 M prints, 9,943 rounds) for research.
- `7c20a7e` v3 research model: training sample fixed (it held only early-round prints).

**Verified**: server on `4687017` at 11:57 UTC; M3 errors 0 after the deploy.

**Research**: v3 model on the full tape found no edge on the 5m/15m crypto lanes (details in ops/experiments.md);
not registered. The scheduled daily agent starts 2026-11-10 06:47 IST.

**Open questions for the owner**
- K1 is the one confirmed lane, but trading it needs a Kalshi account, which is not available from India.
