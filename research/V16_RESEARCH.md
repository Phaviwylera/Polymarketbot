# V16 forward research

V16 adds a shadow research pipeline alongside the existing funded-paper strategy. It does not submit real orders, sign transactions, alter paper risk locks, or automatically promote a model into trading. It is the supervised-learning stage of the proposed architecture; FinRL and FinRobot themselves are not installed in the execution service.

## Runtime

- `poly_features.py`: causal features from Polymarket depth, oracle inputs, signed token trade flow, source-event age, reference age and optional exchange features. Records source, receipt, feature-availability and later label timestamps separately. Missing/stale auxiliary feeds have explicit presence masks.
- `poly_external.py`: public Coinbase spot ticker and Binance USD-M perpetual aggregate-trade/mark-price streams for BTC, ETH, SOL and XRP. Funding is updated from published mark messages. Coinbase USD and Binance USDT remain separate sources; their absolute prices are not treated as identical. Unavailable feeds are reported and retried with backoff. No exchange credentials are needed.
- `poly_shadow.py`: feature loop every 250ms; overlapping probes seeded every ten seconds per side. The seed's features are immutable. Entry waits at least 350ms and rejects an observation later than one second. A full-depth marketable-limit fill must be available within one tick. Stop/target management uses net liquidation value and the asset's configured limits; exit requests latch and include another execution delay. The maximum-hold trigger is 30 seconds, with up to three seconds of executable exit retries. Failed liquidation or a post-entry data gap receives a full entry-cost loss label. These conservative shadow labels are not exchange fills.
- `poly_research_model.py`: no-trade and constant-mean baselines, standardized ridge regression and optional CPU XGBoost. Scaling is fitted on training rows only. Whole UTC market rounds, including all four assets and both sides, stay within chronological train/validation/test partitions. Labels crossing a boundary are purged. Model selection uses validation MSE; a one-sided empirical validation error haircut is applied to the selected model before final test evaluation. It is not a guaranteed confidence interval.
- Automatic baseline training runs in a separate low-priority process every three minutes after 120 labels, with a 60-second timeout. A research candidate requires at least five rounds and ten rows in each partition. The stronger evidence screen requires 20 training, ten validation and ten final-test rounds, at least twenty selected filled test orders, positive net test P&L and a positive round-mean-minus-two-standard-errors screen. Passing this screen still cannot enable execution.
- Evaluation permits one hypothetical selected order per asset per round, respects the funded notional cap including entry fees, and rejects entries with insufficient stop headroom after friction. It does not sum every overlapping probe into a fictitious funded portfolio. It is an offline policy screen, not a full joint-bankroll simulator.

The target is the bounded scalp policy's executable return per share after entry and exit fees, not a five-minute win probability. The existing resolution probability remains a separate, uncalibrated model. Shadow probes continue when paper lanes are loss-locked or the paper session has ended.

## Optional semantic research

RSS headlines are fetched asynchronously every two minutes and must carry a recent publication timestamp. A reachable owner-controlled Ollama server can be configured with `POLY_OLLAMA_URL`; `POLY_OLLAMA_MODEL` defaults to `qwen2.5:7b`. `POLY_RSS_FEEDS` optionally replaces the default RSS sources. V16 does not download or host a language model on the free Render instance.

The JSON estimate must contain bounded probability/uncertainty values and evidence IDs actually supplied in the prompt. Store market identity, publication/receipt/inference times, evidence tags and expiry. Market rollover or expiry makes it inapplicable. LLM self-confidence is not calibration. **Semantic execution weight is always zero**, including when the endpoint works. X/Twitter requires a separate authorized API integration and is not claimed to be connected.

## Offline training

```bash
python -m pip install -r arb-lab-v4/requirements-research.txt
python arb-lab-v4/poly_research_train.py development.zip --output research/candidate.json --xgboost
```

The CPU XGBoost package is optional; runtime baseline training needs only the standard library. XGBoost exports numeric trees to portable JSON and verifies parity against native predictions. No executable pickle is loaded. Older `STATE`-only logs cannot provide these precise delayed-execution labels and are explicitly not fabricated into training outcomes. With insufficient data, the command writes an `insufficient_data` report and no model.

Read-only endpoints:

- `/api/poly/research/v16`: collection, providers, labels, training and per-asset shadow predictions.
- `/api/poly/research/v16/model`: current portable candidate or an explicit no-model response.
- Existing all-asset snapshots include the research panel and each lane's research state.
- Existing development ZIP downloads now include `RESEARCH.ndjson`, preserving sample/entry/outcome links and model evaluations.

## History and storage

Current-session P&L and recorded realized P&L across retained sessions are separate. Known prior losses are archived by session ID; a new session is not a recovered loss. Expired saved sessions retain cash, closed trades and identity instead of automatically being recapitalized at startup.

`POLY_DATA_DIR` selects the directory for paper snapshots, recorded-session history, research journal and model files. Defaults to `/tmp`. Research labels are restored from the journal and models from portable JSON when the directory survives restart. The research journal retains 100,000 critical records; raw lane journals retain their existing independent limits. Render's current filesystem is ephemeral: persistence across replacement instances requires a mounted persistent directory or external storage. The bundled checkpoint and live-instance handover preserve known captured history, not unknown events lost before capture. Download development evidence before a deploy.

## Validation

Regression coverage includes depth/fee arithmetic, causal timestamps, stale/future inputs, asset isolation, duplicate exchange events, entry/exit latency, stop and maximum-hold labels, conservative censored losses, untouched paper cash, chronological partitioning, no-trade comparisons, portable XGBoost inference, RSS grounding, read APIs, research export and session-history preservation. Synthetic test fixtures verify software behavior and are never deployed as trained market models. No profitable strategy is asserted by this release.
