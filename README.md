# eval360-snapshots

Dashboard for tracking LLM evaluation results across checkpoints and datasets. Shows score progression over training and comparison against baseline models.

## Architecture

```
Slurm cluster                        EC2 (100.52.207.136)
┌───────────────────┐                ┌──────────────────────────┐
│ Eval360-V2        │                │ eval360 viewer (FastAPI) │
│ scheduler         │──HTTP POST───▶│ port 11003               │
│                   │                │         │                │
│ dashboard_hook.py │                │         ▼                │
│ (auto-pushes      │                │   RDS Postgres           │
│  after grading)   │                │   (models, checkpoints,  │
└───────────────────┘                │    eval_results)         │
                                     └──────────────────────────┘
backfill.py ──HTTP POST──────────────────────▲        │
(one-time import of                                   │
 existing _scores.yaml)                               ▼
                                              Browser (dashboard UI)
```

- **Auto-ingest**: When an Eval360-V2 job finishes grading, `dashboard_hook.py` POSTs the scores to the viewer. No manual step.
- **Backfill**: The `backfill.py` CLI imports existing `_scores.yaml` files from past eval runs.
- **Viewer**: FastAPI server with Observatory heatmap, radar capability profiles, gap analysis, and training curve charts.

## Quick start (mock demo, no infrastructure needed)

```bash
cd viewer
pip install fastapi uvicorn pydantic
python mock_server.py --port 11001
# Open http://localhost:11001
```

This runs with fake data (3 training models, 3 baselines, 8 benchmarks). Useful for demoing the UI.

## Production deployment on EC2

The viewer runs on the same EC2 instance as the rl360 dashboard (`100.52.207.136`). It shares the rl360 Python venv and connects to the same RDS instance (different database).

### Prerequisites

- SSH access: `ssh -i ~/.ssh/rl360-viewer-key.pem ec2-user@100.52.207.136`
- RDS Postgres instance (already provisioned: `rl-infra.cqpcm6sq0wod.us-east-1.rds.amazonaws.com`)
- A database user with CREATE DATABASE permissions

### Step 1: Clone the repo on EC2

```bash
ssh -i ~/.ssh/rl360-viewer-key.pem ec2-user@100.52.207.136

cd ~/GitHub
git clone git@github.com:LLM360/eval360-snapshots.git
```

Or scp the viewer directory from the cluster:

```bash
scp -i ~/.ssh/rl360-viewer-key.pem -r \
  /path/to/eval360-snapshots/viewer/* \
  ec2-user@100.52.207.136:~/GitHub/eval360-snapshots/viewer/
```

### Step 2: Set up the venv

Reuse the rl360 venv (it has all required deps: fastapi, uvicorn, asyncpg, pydantic, httpx):

```bash
mkdir -p ~/.config/eval360
ln -sfn ~/.config/rl360/venv ~/.config/eval360/venv

# Verify
~/.config/eval360/venv/bin/python -c "import asyncpg, fastapi; print('OK')"
```

Or create a fresh venv:

```bash
python3 -m venv ~/.config/eval360/venv
source ~/.config/eval360/venv/bin/activate
pip install -r ~/GitHub/eval360-snapshots/viewer/requirements.txt
```

### Step 3: Create the database and schema

Since `psql` is not installed on the EC2 instance, use Python:

```bash
source ~/.config/eval360/venv/bin/activate
cd ~/GitHub/eval360-snapshots/viewer

python3 -c "
import asyncio, asyncpg

DB_HOST = 'rl-infra.cqpcm6sq0wod.us-east-1.rds.amazonaws.com'
DB_PORT = 5432
DB_USER = '<username>'
DB_PASS = '<password>'

async def main():
    # Create database
    conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, database='postgres')
    exists = await conn.fetchval(\"SELECT 1 FROM pg_database WHERE datname = 'eval360'\")
    if not exists:
        await conn.execute('CREATE DATABASE eval360')
        print('Created database eval360')
    else:
        print('Database eval360 already exists')
    await conn.close()

    # Apply schema
    conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS, database='eval360')
    await conn.execute(open('schema.sql').read())
    tables = await conn.fetch(\"SELECT tablename FROM pg_tables WHERE schemaname = 'public'\")
    print('Tables:', [t['tablename'] for t in tables])
    await conn.close()

asyncio.run(main())
"
```

### Step 4: Write the env file

```bash
# Generate an ingest token
INGEST_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
echo "Save this token: $INGEST_TOKEN"

# Write env file (URL-encode special characters in the password)
cat > ~/.config/eval360/env << EOF
DATABASE_URL=postgresql://<user>:<url-encoded-password>@rl-infra.cqpcm6sq0wod.us-east-1.rds.amazonaws.com:5432/eval360
INGEST_TOKEN=$INGEST_TOKEN
EOF
```

If the password has special characters (`#`, `$`, `[`, `:`, etc.), URL-encode them:

```python
python3 -c "from urllib.parse import quote; print(quote('your-password-here', safe=''))"
```

### Step 5: Install and start the systemd service

```bash
# Copy service file
cp ~/GitHub/eval360-snapshots/viewer/systemd/eval360-viewer.service ~/.config/systemd/user/

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now eval360-viewer

# Verify
systemctl --user status eval360-viewer
journalctl --user -u eval360-viewer -f
```

The viewer runs on **port 11003** (rl360 uses 11001, team-sessions uses 11002).

### Step 6: Access the dashboard

From the Slurm cluster, use an SSH tunnel:

```bash
ssh -i ~/.ssh/rl360-viewer-key.pem -f -N -L 29876:localhost:11003 ec2-user@100.52.207.136
# Open http://localhost:29876 in your browser
```

For permanent access without a tunnel, set up a Cloudflare DNS record (e.g., `eval360.llm360.ai`) pointing to the EC2 instance, with a Cloudflare Access bypass for `/api/ingest` (same pattern as rl360 — see the [rl360 viewer README](https://github.com/LLM360/rl360-snapshots/blob/main/viewer/README.md#cloudflare-access-and-the-ingest-api)).

## Eval360-V2 auto-ingest

When the dashboard is running, set these env vars in your Eval360-V2 scheduler environment to auto-push scores after every eval:

```bash
# If using SSH tunnel:
export EVAL360_DASHBOARD_URL=http://localhost:29876
# If using Cloudflare DNS:
# export EVAL360_DASHBOARD_URL=https://eval360.llm360.ai

export EVAL360_DASHBOARD_TOKEN=<your-ingest-token>
```

Then run evals as normal:

```bash
conda activate eval360-scheduler
eval360 --max-generation-jobs 1 --max-grading-parallelism 20 \
  evaluate-now \
  --model-paths <model.yaml> \
  --data-paths <data.yaml>
```

When grading completes, `scheduler/dashboard_hook.py` automatically POSTs the scores. The hook is a no-op when `EVAL360_DASHBOARD_URL` is unset — no impact on existing workflows.

## Backfill existing results

Import `_scores.yaml` files from past eval runs:

```bash
# Training model (multiple checkpoints)
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/k2-think-v2-checkpoint-5/ \
  --model-id k2-think-v2 \
  --model-type training \
  --owner varad \
  --dashboard-url http://localhost:29876 \
  --token <ingest-token>

# Baseline model (single eval)
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/gpt-4o/ \
  --model-id gpt-4o \
  --model-type baseline \
  --owner external \
  --dashboard-url http://localhost:29876 \
  --token <ingest-token>
```

Optional flags: `--training-step N`, `--checkpoint-id ID`, `--primary-metric NAME`.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Postgres connection string (URL-encode special chars in password) |
| `INGEST_TOKEN` | Yes | Bearer token for ingest auth |

## Managing the service

```bash
ssh -i ~/.ssh/rl360-viewer-key.pem ec2-user@100.52.207.136

systemctl --user status eval360-viewer       # check status
systemctl --user restart eval360-viewer      # restart after code changes
systemctl --user stop eval360-viewer         # stop
journalctl --user -u eval360-viewer -f       # tail logs
journalctl --user -u eval360-viewer --since "1 hour ago"  # recent logs
```

## EC2 services layout

| Service | Port | Purpose |
|---------|------|---------|
| `rl360-viewer` | 11001 | RL training dashboard |
| `team-sessions` | 11002 | Team sessions API |
| `eval360-viewer` | 11003 | Eval dashboard (this project) |
