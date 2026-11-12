# Evolution log

Newest first. One entry per daily run: findings, fixes (with commits), verification, open questions for the owner.

## 2026-11-12 (daily agent)

**Findings** (review at 01:20 UTC, revision `689721d`, window 24 h, uptime 23.9 h)
- `daily_review.py` flagged "rivals: runs did not move (4)". False alarm: the server restarts about 01:25 UTC every day
  (the daily agent's push), and rivals runs every 6 h, so both boots ended at runs = 4; its `lastRunAt` moved from
  11-10 19:42 to 11-11 19:42 UTC and it has 0 errors. The review tool compared counters across a reboot. This would
  have repeated daily and also hid error spikes after a restart. Fixed below.
- All lanes 0 errors. Storage healthy: 183 transient write errors this boot (207 last boot), 0 dropped, backup OK.
- Memory: RSS 1,125 MB (was 1,184), 376 MB free. Loop stalls ~1.2-1.5 s every ~2 min, unchanged. Watch.
- Jev/H2 (failed): still OpenRouter 402. Bullpen: 380 of 756 calls `UNCLASSIFIED_ERROR` (external, unchanged).
- M3 count checked: 40 market-days on 12 Nov 01:20 UTC (two full UTC days of ~20 markets), consistent.

**Fixed**
- `739f133` `daily_review.py`: when the server restarted inside the window, errors are compared with 0 and a lane is
  flagged stalled only if its activity counter is still 0 two hours after boot. Tests fail before, pass after; full
  suite 378 passed. No bot code or strategy changed.

**Verified**: see the line below once the server shows the new revision.

**Lanes** (24 h)
- K1: 1,323 rounds (+88), bad case +0.36c, still confirmed on paper. Kalshi not usable from India.
- M3: 40 of 50 market-days, -0.03c per market-day, bad case -0.41c; collecting. Likely reaches 50 within about a day.
- S2 (failed): 3,220 trades (+370), -0.05c per $, bad case -1.06c.
- X1 (failed): 1,019 events, bad case -34%. M2 bad case -17%. M1, R1, H2: failed, recording only. E1, N1: collecting,
  no samples yet.
- No verdict reached its sample size today. No new experiment (gate not used).

**Open questions for the owner** (unchanged)
- Jev/H2 has failed its test and only produces 402 errors. Top up OpenRouter, or switch it off?
- K1 remains the only confirmed lane, still blocked by Kalshi access from India.

## 2026-11-11 (daily agent)

**Findings** (review at 01:11 UTC, revision `c1a6d91`, window 24 h, uptime 23.6 h)
- `daily_review.py`: no findings. Server on main, status fresh, every lane's activity counter moving.
- X1 (failed lane): 18 errors, all Deribit `503 Service Unavailable` on `get_book`; external and transient, not changed.
- Storage: 51 transient write errors in 24 h (156 -> 207 this boot), 0 dropped events, last error none, backup OK;
  same rate as before, not a fault.
- Memory: process RSS 1,184 MB, flat since 2026-11-10 16:00 UTC; 317 MB free on the t4g.small (was ~430 MB). Loop
  stalls of about 1.2 s every 2 min (GC-linked) unchanged. Watch, not a fault yet.
- Jev/H2 (failed): still OpenRouter 402 Payment Required. Bullpen study: 332 of 660 calls `UNCLASSIFIED_ERROR`
  (unchanged, external).
- M3 count checked: 21 market-days ~25 h after `REGISTERED_AT` on ~20 quoted markets is consistent with the fixed
  scoring window.

**Fixed**: nothing (no faults).

**Lanes** (24 h)
- K1: 1,235 rounds (+77), +1.14c per pair, bad case +0.28c, still confirmed on paper. Kalshi not usable from India.
- M3: 21 of 50 market-days, -0.29c per market-day, bad case -0.99c; collecting.
- S2 (failed): 2,850 trades (+408), -0.10c per $, bad case -1.17c.
- X1 (failed): 951 events, bad case -36%. M1, M2, R1, H2: failed, recording only. E1, N1: collecting, no samples yet.
- No verdict reached a sample size today. No new experiment (gate not used).

**Open questions for the owner** (unchanged)
- Jev/H2 has failed its test and only produces 402 errors. Top up OpenRouter, or switch it off?
- K1 remains the only confirmed lane, still blocked by Kalshi access from India.

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
