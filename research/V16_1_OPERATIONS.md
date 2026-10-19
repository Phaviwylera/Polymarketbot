# V16.1 — durable evidence and connected market telemetry

The existing paper entry conditions, allocations, stops and take-profit rules remain unchanged. No real order endpoints or private trading keys are used. ML and headline features remain shadow-only.

## Persistence and ownership

Set `POLY_DATABASE_URL` (or `DATABASE_URL`) on the engine only. `asyncpg` connects privately to the existing Render database. Tables are namespaced `poly_v161_state` and `poly_v161_events`; existing unrelated tables are untouched. The URL never enters status or logs. `POLY_DATABASE_EXPIRES_AT` displays a plan-expiry notice.

One session-level PostgreSQL advisory lock owns paper execution. A rolling replacement can respond in standby with its last checkpoint while waiting for the previous owner to drain. No feed or trading workers start in standby. When the owner connection is lost, the old worker tasks are cancelled before reconnecting. An unhealthy checkpoint or dropped audit queue blocks new entries without permanently applying a portfolio loss lock. Ordinary position exit checks continue while the connection is usable. This is a single-instance paper engine, not an exactly-once live exchange executor.

Checkpoints, event inserts and model saves share a transaction. Event IDs use journal boot ID plus sequence; retries are idempotent. Checkpoints run approximately every two seconds; sudden process loss can lose changes since the last successful transaction. Status exposes actual checkpoint age and queue health rather than promising zero data loss. Shutdown flushes while holding ownership.

Store: current paper state, retained prior-session totals, shadow outcome labels, current compatible portable model, observed trade attribution, sampled decisions and execution evidence. Raw CLOB/RTDS logs and fine-grained samples remain bounded local SQLite spools. There is no claim of permanent tick-level replay. Outcome retention is 60,000; training loads the latest 12,000; decisions retain 10,000; attribution view retains the latest 5,000 closes. Raw local logs must still be downloaded before deployments.

`POLY_RESEARCH_BOOTSTRAP_B64` optionally imports gzip/base64 `{rows:[...]}` from retained V16 diagnostics into an empty database. IDs make importing labels idempotent. The existing private paper checkpoint bootstraps the initial ledger; after that the database is used for release-independent recovery. No live ledger or imported labels belong in public Git.

## Attribution

Each observed position close records entry and exit prices, quantities, actual simulated fees, gross/net return reconciliation, hold time, entry latency/slippage, exit latency, entry features, exit reason and stop overshoot. The headline total ledger is separate from this V16.1-only coverage; missing historical detail is not fabricated. Complete-set/inventory event evidence remains in the audit journal and is not presented as fully reconstructed position attribution.

`GET /api/poly/operations` exposes storage and attribution summaries. The snapshot includes these fields. Dev ZIPs include `execution-attribution.json` and `storage-status.json` alongside all lane and research journals.

## Feeds

Binance aggregate trades and mark prices now use the officially documented `/market/stream` path. Provider and per-asset event ages, reconnect counts and availability masks remain explicit. No proxy or geographic bypass is used. Unavailable features remain absent.

## Interface and orb

System sans-serif typography, tabular numeric spacing, compact rounded buttons and responsive modal styles follow the uploaded reference's typography and control shapes while preserving the existing dark palette and asset colors. All four assets remain in one view.

Each fresh, changed order-book observation can emit one bounded visual packet. Its asset color and source remain fixed. Packets follow a cubic inlet ending at a specific projected sphere node, then traverse actual neighboring network edges. Lines and packets use the same great-circle interpolation and projection, including node pulse deformation. There are no free-floating signal dots. Node arrivals produce damped visual energy, not a claim of physical market causality or real order routing. Reduced motion, offscreen pause and stale feeds stop emissions; hidden views do not accumulate old packets. Animation data never drives execution.
