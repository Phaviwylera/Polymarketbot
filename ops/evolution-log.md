# Evolution log

Newest first. One entry per daily run: findings, fixes (with commits), verification, open questions for the owner.

## 2026-11-10 (daily agent)

**Findings** (review at 01:06 UTC, revision `71753f9`, window 24 h)
- `daily_review.py`: no findings; every lane 0 errors; storage healthy; server on main.
- Impossible number: M3 showed 24 market-days 66 minutes after its 00:00 UTC re-registration. Cause: `report()` kept a
  `REGISTERED_AT - 86400` slack (left from the old local-time `mktime` check), so all of 2026-11-09 counted, part of it
  from before the survivorship / Data API v2 fix.
- Jev (H2, already failed): 1,889 of 1,889 OpenRouter calls in 24 h answered 402 Payment Required (credits used up).
  External billing, not a code fault; not changed.
- Bullpen wallet study: about 40% of calls fail with `trader-profile: UNCLASSIFIED_ERROR` (same as yesterday);
  read-only study, cause inside the external tool; not changed.
- Event loop stalls of about 1.2 s every 2 minutes (GC-linked) and ~430 MB free RAM on the t4g.small; steady, watch.

**Fixed**
- `680798d` M3 scores only UTC days from `REGISTERED_AT` on. Rule, pass mark and sample size unchanged. New test
  fails before, passes after; full suite 376 passed.

**Verified**: server on `680798d` at 01:24 UTC; all lanes 0 errors, storage healthy; M3 now 4 market-days
(+5.4c mean, bad case -1.0c), collecting.

**Lanes** (24 h)
- K1: 1,158 rounds (+64), bad case +0.04c, still confirmed on paper. Kalshi not usable from India.
- S2 (failed): 2,442 trades (+415), -0.44c per $, bad case -1.6c.
- M3: restarted cleanly today, 4 of 50 market-days.
- X1 (failed): 884 events, bad case -35%. M1, M2, R1: failed, recording only. E1, N1: collecting, no samples yet.
- No new experiment (gate not used).

**Open questions for the owner**
- Jev/H2 has failed its test and now only produces 402 errors. Top up OpenRouter, or switch it off?
- K1 remains the only confirmed lane, still blocked by Kalshi access from India.

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
