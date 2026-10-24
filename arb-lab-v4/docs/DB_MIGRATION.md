# Database backup, migration and restore

The current Render free Postgres (`poly-edge-state`) expires on **2026-11-17**. Migrate and prove a
restore before **2026-11-10**. Never paste a connection string into chat, code, commits or logs; set it
only in the Render dashboard (Environment).

## What is stored

| Table | Contents |
|---|---|
| `poly_v161_state` | one row per name: `paper` (portfolio checkpoint incl. risk state, open positions, ledger history), `edge` (EdgeLab bets, calibration, wallets), `model` (research model artifact), `baseline_2026_10_25` (frozen baseline, written once) |
| `poly_v161_events` | append-only evidence: `TRADE_ATTRIBUTION` (every closed trade with fees, latency, stop evidence, experiments), `SHADOW_OUTCOME` (research labels), `DECISION_SAMPLE`, `EXIT_ATTEMPT`, `MOMENTUM_ORDER`, pair events |

Constraints: `poly_v161_state.name` primary key; `poly_v161_events.id` primary key
(`bootId:journalSeq`, so retries never duplicate); index `(kind, t DESC)`.

## Built-in verified copy (`poly_dbcopy.py`)

Setting `POLY_BACKUP_URL` on the service makes the running bot copy the primary database to that
database 10 minutes after start and then daily, and log one `POLY_BACKUP {...}` line (no URLs):

* every state row upserted, every missing event inserted (keyset batches of 2,000; the source is only read);
* row counts and checksums (count, sum and max of `t`) per event kind compared with the source;
* the paper checkpoint's portfolio totals (equity, realized P&L, trades, risk lock, drawdown) compared;
* **restore check**: the exact reads a restart performs (`paper`, `edge`, `model`, 12,000 labels,
  5,000 trade attributions) run on the target and parse.

`"ok":true` is required. The dashboard's ALERTS row shows "no verified backup recorded yet" until one
succeeds. A target equal to the primary is refused.

## Migration procedure

1. **Create the new database** (e.g. Supabase free: no card, 500 MB, pauses only after a week without
   activity, which a bot writing every 5 s never reaches). Copy its connection string (use the
   *session* pooler or direct connection, not the transaction pooler: the bot holds an advisory lock).
2. **Back up first.** In Render → arb-lab-v4-unified → Environment add `POLY_BACKUP_URL` = new
   database. Save (the service redeploys).
3. **Wait for the copy** (~10 min after boot). In the logs find `POLY_BACKUP` and confirm
   `"ok":true`, `mismatchedKinds:[]`, `portfolioMatches:true` and `restoreCheck.paper` = `ok`.
   Note `stateRows` and `eventRows`.
4. **Switch.** Change `POLY_DATABASE_URL` to the new database and set `POLY_BACKUP_URL` to the **old**
   one (the old database becomes the backup target until it expires). Save.
5. **Verify after the switch**, from the logs and dashboard:
   * `POLY_STORAGE_ERROR` absent; dashboard storage state `healthy`;
   * equity, account P&L, lifetime trades, max drawdown and risk-lock state equal the values noted in step 3;
   * an open paper position (if any) is still open with the same entry; guard/pause state unchanged;
   * a new trade appears in the trade CSV and the next `POLY_AUDIT` ledger shows 0 problem sessions;
   * restart once more (Manual Deploy) and re-check the same totals (restart recovery).
6. **Rollback** at any point before the old database expires: set `POLY_DATABASE_URL` back to the old
   database. Nothing was ever written to the old database by the copy, so it is exactly as it was,
   plus whatever the bot wrote while it was primary.

## Local test

`test_dbcopy.py` runs the copy against two disposable local databases:

```
POLY_TEST_PG_SRC=postgresql://postgres@/src?host=/tmp/pgtest&port=55432 \
POLY_TEST_PG_DST=postgresql://postgres@/dst?host=/tmp/pgtest&port=55432 python3 -m pytest test_dbcopy.py
```

It checks a full copy (counts, checksums, portfolio, restore reads), an incremental second run, and that a
target that diverged is detected (`ok:false`).
