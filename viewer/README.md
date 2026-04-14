# Eval360 Viewer

FastAPI server that serves the eval dashboard UI and provides query/ingest APIs over Postgres-backed eval results.

## Setup

### 1. Create venv and install deps

```bash
python3.12 -m venv ~/.config/eval360/venv
source ~/.config/eval360/venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
mkdir -p ~/.config/eval360
cp systemd/env.example ~/.config/eval360/env
# Edit ~/.config/eval360/env with your DATABASE_URL and INGEST_TOKEN
```

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string (`postgresql://user:pass@host:5432/eval360`) |
| `INGEST_TOKEN` | Yes | Bearer token for ingest auth (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) |

If the database password has special characters, URL-encode them:

```bash
python3 -c "from urllib.parse import quote; print(quote('your-password', safe=''))"
```

### 3. Initialize Postgres schema

```bash
source ~/.config/eval360/env
psql $DATABASE_URL -f schema.sql
```

### 4. Run

```bash
source ~/.config/eval360/env
python server.py --port 11003
```

## Persistent deployment (systemd)

```bash
mkdir -p ~/.config/systemd/user
cp systemd/eval360-viewer.service ~/.config/systemd/user/
# Edit the service file paths for your venv and repo clone

systemctl --user daemon-reload
systemctl --user enable --now eval360-viewer

# Check status
systemctl --user status eval360-viewer
journalctl --user -u eval360-viewer -f
```

## Shared domain setup (nginx + Cloudflare)

The eval360 viewer shares an EC2 instance and Cloudflare domain with the rl360 viewer:

```
Browser -> Cloudflare DNS proxy (dashboard.llm360.ai) -> EC2 nginx -> eval360 (port 11003)
                                                                    -> rl360  (port 11001)
```

Nginx routes by path prefix (`/eval360/` and `/rl360/`). See the [rl360 viewer README](https://github.com/LLM360/rl360-snapshots/blob/main/viewer/README.md#shared-domain-setup) for the full nginx and Cloudflare configuration.

### Cloudflare Access and the ingest API

The ingest endpoint (`/eval360/api/ingest/*`) must be bypassed in Cloudflare Access so that the Eval360-V2 scheduler can POST scores from the Slurm cluster without SSO. Create a Cloudflare Access application:

- **`dashboard.llm360.ai/eval360/api/ingest`**: Action=**Bypass**, Include=Everyone.

If ingest stops working (HTTP 302), check that the Bypass application still exists in the Cloudflare Zero Trust dashboard.

## Schema

The database has 11 tables across 5 phases:

| Table | Phase | Purpose |
|-------|-------|---------|
| `models` | 0 | Model families |
| `checkpoints` | 0 | Evaluable units (per-step or baseline) |
| `eval_results` | 0 | Per-(checkpoint, dataset, metric) scores |
| `eval_runs` | 1 | Evaluation execution provenance |
| `benchmark_metadata` | 3 | Dataset categories and descriptions |
| `eval_suites` | 3 | Named collections of datasets |
| `example_results` | 4 | Per-example correctness and previews |
| `alerts` | 5 | Automated regression/improvement notifications |
| `promotion_rules` | 5 | Checkpoint readiness criteria |
| `activity_log` | 5 | Audit trail of dashboard events |
| `webhooks` | 5 | HTTP callbacks on events |

Run `psql $DATABASE_URL -f schema.sql` to create or update all tables. The schema is additive — each phase appends new tables/columns without modifying existing ones.

## Files

| File | Purpose |
|------|---------|
| `server.py` | Production FastAPI server (requires Postgres) |
| `mock_server.py` | Mock server with in-memory seed data (no Postgres) |
| `db.py` | asyncpg pool and query helpers |
| `schema.sql` | Postgres DDL for all tables |
| `index.html` | Single-page dashboard frontend |
| `backfill.py` | CLI to import existing `_scores.yaml` files |
| `requirements.txt` | Python dependencies |
| `systemd/` | Service file and env template |
