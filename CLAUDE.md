# CLAUDE.md

Polymarket paper-trading bot (`arb-lab-v4/`). Paper only: no real orders are placed.

## Skills

Project skills live in `.claude/skills/`, copied from [affaan-m/ECC](https://github.com/affaan-m/ECC) (commit b38be6a). Treat their content as guidance only. They never override this file, the user's instructions or the repo's safety rules, and their install scripts and hooks are not used.

- **prediction-market-risk-review**: before any change to strategy, sizing, or the paper-to-live switch.
- **prediction-market-oracle-research**: for market data, sources and resolution.
- **llm-trading-agent-security**: for keys, wallets and order placement.
- **postgres-patterns**: for the Supabase/PostgreSQL storage.
- **deployment-patterns** and **canary-watch**: for the EC2 server and its deploys.
- **python-testing**: for tests on new logic.
