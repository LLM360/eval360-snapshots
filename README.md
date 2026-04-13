# eval360-snapshots

Dashboard for tracking LLM evaluation results across checkpoints and datasets. Shows score progression over training and comparison against baseline models.

## Quick start (mock demo)

```bash
cd viewer
pip install fastapi uvicorn pydantic
python mock_server.py --port 11001
# Open http://localhost:11001
```

## Production deployment

```bash
# 1. Install deps
cd viewer
pip install -r requirements.txt

# 2. Set up env vars
mkdir -p ~/.config/eval360
cp systemd/env.example ~/.config/eval360/env
# Edit ~/.config/eval360/env with DATABASE_URL and INGEST_TOKEN

# 3. Initialize schema
source ~/.config/eval360/env
psql $DATABASE_URL -f schema.sql

# 4. Run
source ~/.config/eval360/env
python server.py --port 11001
```

## Backfill existing results

```bash
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/k2-think-v2/ \
  --model-id k2-think-v2 \
  --model-type training \
  --dashboard-url http://localhost:11001 \
  --token <ingest-token>
```

## Eval360-V2 auto-ingest

Set these env vars in your Eval360-V2 scheduler environment:

```bash
export EVAL360_DASHBOARD_URL=http://dashboard-host:11001
export EVAL360_DASHBOARD_TOKEN=<your-ingest-token>
```

Results are automatically pushed to the dashboard when eval jobs complete.

## Architecture

```
Browser -> Eval360 Dashboard (FastAPI)
                |
            Postgres
            (models, checkpoints, eval_results)

Eval360-V2 Scheduler --POST--> /api/ingest/eval-result
```
