# eval360-snapshots

Dashboard for tracking LLM evaluation results across checkpoints and datasets. Shows score progression over training, radar capability profiles, gap analysis against baselines, and cross-model heatmaps.

## Architecture

```
Slurm cluster                          EC2 instance
┌────────────────────┐                ┌───────────────────────────┐
│ Eval360-V2         │                │ eval360 viewer (FastAPI)  │
│ scheduler          │──HTTP POST───▶│ port 11003                │
│                    │                │         │                 │
│ dashboard_hook.py  │                │         ▼                 │
│ (auto-pushes       │                │   RDS Postgres            │
│  after grading)    │                │   (models, checkpoints,   │
└────────────────────┘                │    eval_results)          │
                                      └───────────────────────────┘
backfill.py ─HTTP POST──────────────────────▲        │
(one-time import of                                  ▼
 existing _scores.yaml)                       Browser (dashboard UI)
```

## Quick start (mock demo)

```bash
cd viewer
pip install fastapi uvicorn pydantic
python mock_server.py --port 11001
# Open http://localhost:11001
```

## Production deployment

```bash
# 1. Clone
git clone git@github.com:LLM360/eval360-snapshots.git
cd eval360-snapshots/viewer

# 2. Create venv and install deps
python3.12 -m venv ~/.config/eval360/venv
source ~/.config/eval360/venv/bin/activate
pip install -r requirements.txt

# 3. Set up env vars (copy template and fill in values)
mkdir -p ~/.config/eval360
cp systemd/env.example ~/.config/eval360/env
# Edit ~/.config/eval360/env with your DATABASE_URL and INGEST_TOKEN

# 4. Initialize Postgres schema
source ~/.config/eval360/env
psql $DATABASE_URL -f schema.sql

# 5. Run the viewer
source ~/.config/eval360/env
python server.py --port 11003
# Open http://localhost:11003 in your browser
```

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string (`postgresql://user:pass@host:5432/eval360`) |
| `INGEST_TOKEN` | Yes | Bearer token for ingest auth (generate with `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`) |

If the database password has special characters (`#`, `$`, `[`, `:`, etc.), URL-encode them in the connection string:

```bash
python3 -c "from urllib.parse import quote; print(quote('your-password', safe=''))"
```

## Persistent deployment (systemd)

For always-on deployment on EC2 (or any Linux host):

```bash
# Install systemd user service
mkdir -p ~/.config/systemd/user
cp systemd/eval360-viewer.service ~/.config/systemd/user/

# Edit the service file to set correct paths for your venv and repo clone

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now eval360-viewer

# Check status
systemctl --user status eval360-viewer
journalctl --user -u eval360-viewer -f
```

### EC2 services layout

| Service | Port | Purpose |
|---------|------|---------|
| `rl360-viewer` | 11001 | RL training dashboard |
| `team-sessions` | 11002 | Team sessions API |
| `eval360-viewer` | 11003 | Eval dashboard (this project) |

### Accessing from the Slurm cluster

The EC2 security group doesn't expose port 11003 directly. Use an SSH tunnel:

```bash
ssh -i ~/.ssh/rl360-viewer-key.pem -f -N -L 29876:localhost:11003 ec2-user@100.52.207.136
# Dashboard is now at http://localhost:29876
```

For permanent access, set up a Cloudflare DNS record (e.g., `eval360.llm360.ai`) with a Cloudflare Access bypass for `/api/ingest`, same pattern as the [rl360 viewer](https://github.com/LLM360/rl360-snapshots/blob/main/viewer/README.md#cloudflare-access-and-the-ingest-api).

## Eval360-V2 auto-ingest

Set these env vars in your Eval360-V2 scheduler environment:

```bash
export EVAL360_DASHBOARD_URL=http://localhost:29876   # via SSH tunnel
export EVAL360_DASHBOARD_TOKEN=<your-ingest-token>
```

Results are automatically pushed to the dashboard when eval jobs complete. The hook is a no-op when these vars are unset.

## Backfill existing results

```bash
# Training model (multiple checkpoints)
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/k2-think-v2/ \
  --model-id k2-think-v2 \
  --model-type training \
  --dashboard-url http://localhost:29876 \
  --token <ingest-token>

# Baseline model (single eval)
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/gpt-4o/ \
  --model-id gpt-4o \
  --model-type baseline \
  --dashboard-url http://localhost:29876 \
  --token <ingest-token>
```

## API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI |
| `GET /api/models` | List all models (filters: `model_type`, `owner`) |
| `GET /api/models/{id}` | Model detail + checkpoints |
| `GET /api/models/{id}/scores` | All eval results for a model |
| `GET /api/models/{id}/diagnosis` | Gap analysis vs baselines + trend indicators |
| `GET /api/checkpoints/{id}` | Checkpoint detail + results |
| `GET /api/datasets` | List dataset names |
| `GET /api/datasets/{name}/leaderboard` | Ranked by best primary metric |
| `GET /api/heatmap` | All models × all datasets matrix |
| `GET /api/compare` | Multi-model comparison (`models`, `dataset` params) |
| `GET /api/filters` | Distinct values for dropdowns |
| `POST /api/ingest/eval-result` | Ingest scores (requires Bearer token) |
