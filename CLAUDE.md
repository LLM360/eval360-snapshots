# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## Project Overview

eval360-snapshots is a FastAPI dashboard for tracking LLM evaluation results across checkpoints and datasets. It serves as the eval counterpart to [rl360-snapshots](https://github.com/LLM360/rl360-snapshots) (RL training dashboard). Both share an EC2 instance and are served under `dashboard.llm360.ai`.

## Architecture

```
Eval360-V2 scheduler → POST /api/ingest/eval-result → FastAPI server → Postgres (RDS)
                                                                    → Browser (SPA)
```

- **Backend:** FastAPI + asyncpg, single `server.py` file
- **Frontend:** Single-file SPA (`index.html`) with Chart.js, no build step
- **Storage:** Postgres for aggregates + metadata, Weka for full example text
- **Mock:** `mock_server.py` runs with fake data, no Postgres needed
- **Deployment:** systemd on EC2, nginx reverse proxy, Cloudflare DNS

## Key Files

| File | Purpose |
|------|---------|
| `viewer/server.py` | FastAPI app: ingest, query, admin endpoints |
| `viewer/db.py` | asyncpg pool + upsert/query helpers |
| `viewer/schema.sql` | Postgres schema (additive migrations appended to end) |
| `viewer/index.html` | Full SPA frontend (~2400 lines) |
| `viewer/mock_server.py` | Standalone demo with fake data |
| `viewer/backfill.py` | CLI to import existing _scores.yaml files |

## Schema

5 tables: `models`, `checkpoints`, `eval_results`, `eval_runs` (provenance), `example_results` (drill-down). Plus `benchmark_metadata` and `eval_suites` for taxonomy.

Schema migrations are **additive** — new tables and ALTER TABLE statements are appended to `schema.sql`. Never modify existing CREATE TABLE statements.

## Development Patterns

- **Schema changes:** Append to end of `schema.sql`. Run via asyncpg on EC2 (no psql needed).
- **API changes:** Update both `server.py` (Postgres) and `mock_server.py` (in-memory) to keep them in sync.
- **Frontend changes:** Edit `index.html` directly. Use Edit tool, never full rewrites. Verify script tag balance after edits.
- **Deployment:** Push branch, deploy to EC2 with `git fetch && git checkout origin/<branch> --detach && systemctl --user restart eval360-viewer`.
- **Ingest API:** All new fields must be optional for backwards compatibility.

## Shared Domain Setup

Both dashboards served under `dashboard.llm360.ai`:

| Path | Service | Port |
|------|---------|------|
| `/rl360/` | RL360 dashboard | 11001 |
| `/eval360/` | Eval360 dashboard (this project) | 11003 |

Frontend auto-detects its base path via `BASE_PATH` in JS. API calls are prefixed accordingly.

## Testing

```bash
# Run mock server
cd viewer && python mock_server.py --port 11001

# Verify Python syntax
python -c "import ast; ast.parse(open('viewer/server.py').read()); print('OK')"

# Verify HTML integrity
python -c "html=open('viewer/index.html').read(); print(f'script: {html.count(\"<script\")}={html.count(\"</script>\")}')"
```

## Key Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | Postgres connection string |
| `INGEST_TOKEN` | Bearer token for ingest/admin auth |

Dashboard URL/token for Eval360-V2 hook: stored at `/mnt/weka/shrd/k2m/eval360/dashboard.env`.
