# eval360-snapshots

Dashboard for tracking LLM evaluation results across checkpoints and datasets. Shows score progression over training, radar capability profiles, gap analysis against baselines, and cross-model heatmaps.

## Quick start (mock demo)

```bash
# 1. Clone
git clone git@github.com:LLM360/eval360-snapshots.git
cd eval360-snapshots/viewer

# 2. Install deps and run
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

## Architecture

```
Browser -> Cloudflare DNS proxy (dashboard.llm360.ai/eval360/) -> EC2 -> eval360 viewer (FastAPI)
                                                                               |
                                                                          Postgres (RDS)
                                                                          (models, checkpoints,
                                                                           eval_results)
```

- **Ingest**: Eval360-V2 scheduler POSTs scores to the viewer via `POST /api/ingest/eval-result`
- **Backfill**: `backfill.py` CLI imports existing `_scores.yaml` files from past eval runs
- **Views**: Observatory heatmap, model radar profiles, gap analysis, training curves, leaderboard

The eval360 viewer shares an EC2 instance and Cloudflare domain with the [rl360 viewer](https://github.com/LLM360/rl360-snapshots). Both are served under `dashboard.llm360.ai`:

| Path | Service | Port |
|------|---------|------|
| `/rl360/` | RL360 training dashboard | 11001 |
| `/eval360/` | Eval360 eval dashboard (this project) | 11003 |

Nginx on the EC2 instance routes by path prefix. See the [rl360 viewer README](https://github.com/LLM360/rl360-snapshots/blob/main/viewer/README.md#shared-domain-setup) for the full nginx and Cloudflare Access configuration.

### Cloudflare Access and the ingest API

The ingest endpoint (`/eval360/api/ingest/*`) must be bypassed in Cloudflare Access so that the Eval360-V2 scheduler (running on the Slurm cluster) can POST scores without SSO. The working setup is a Cloudflare Access application:

- **`dashboard.llm360.ai/eval360/api/ingest`**: Action=**Bypass**, Include=Everyone.

If ingest stops working and the scheduler reports HTTP 302, check that the Bypass application still exists in the Cloudflare Zero Trust dashboard.

## Eval360-V2 auto-ingest

Enable dashboard logging via CLI flags when running eval:

```bash
eval360 --max-generation-jobs 1 --max-grading-parallelism 20 \
  evaluate-now \
  --model-paths model.yaml \
  --data-paths data.yaml \
  --dashboard-logging \
  --dashboard-logging-examples    # optional: also log per-example results
```

- `--dashboard-logging` — POST aggregate scores to the dashboard after grading
- `--dashboard-logging-examples` — also POST per-example results (input/output previews, correctness, slice labels)

No env vars needed — the dashboard URL and token are read from a shared config file on Weka (`/mnt/weka/shrd/k2m/eval360/dashboard.env`).

The hook is implemented in `scheduler/dashboard_hook.py` in the [Eval360-V2 repo](https://github.com/LLM360/Eval360-V2).

### Advanced: override dashboard URL

Env vars take precedence over the shared config file if set:

```bash
export EVAL360_DASHBOARD_URL=https://dashboard.llm360.ai/eval360
export EVAL360_DASHBOARD_TOKEN=<your-ingest-token>
```

## Backfill existing results

Import `_scores.yaml` files from past eval runs:

```bash
# Training model (multiple checkpoints)
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/k2-think-v2/ \
  --model-id k2-think-v2 \
  --model-type training \
  --dashboard-url https://dashboard.llm360.ai/eval360 \
  --token <ingest-token>

# Baseline model (single eval)
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/gpt-4o/ \
  --model-id gpt-4o \
  --model-type baseline \
  --dashboard-url https://dashboard.llm360.ai/eval360 \
  --token <ingest-token>
```

## API endpoints

### Query
| Endpoint | Description |
|----------|-------------|
| `GET /` | Dashboard UI |
| `GET /api/models` | List all models (filters: `model_type`, `owner`) |
| `GET /api/models/{id}` | Model detail + checkpoints |
| `GET /api/models/{id}/scores` | All eval results (with CIs) for a model |
| `GET /api/models/{id}/diagnosis` | Gap analysis, significance, trends, best checkpoints |
| `GET /api/checkpoints/{id}` | Checkpoint detail + results |
| `GET /api/datasets` | List dataset names |
| `GET /api/datasets/{name}/leaderboard` | Ranked by best primary metric |
| `GET /api/heatmap` | Models × datasets matrix with status + coverage + categories |
| `GET /api/compare` | Multi-model comparison (supports `?common_only=true`) |
| `GET /api/filters` | Distinct values for dropdowns |
| `GET /api/suites` | List evaluation suites |
| `GET /api/suites/{id}/heatmap` | Suite-filtered heatmap |
| `GET /api/eval-runs/{id}` | Eval run provenance details |
| `GET /api/eval-runs/{id}/examples` | Paginated examples (filters: `correct`, `topic`, `difficulty`) |
| `GET /api/eval-runs/{id}/slices` | Slice analysis by topic + difficulty |

### Ingest (requires Bearer token)
| Endpoint | Description |
|----------|-------------|
| `POST /api/ingest/eval-result` | Ingest scores with provenance |
| `POST /api/ingest/examples` | Bulk ingest example-level results |

### Admin (requires Bearer token)
| Endpoint | Description |
|----------|-------------|
| `POST /api/admin/suites` | Create/update evaluation suite |
| `POST /api/admin/benchmark-metadata` | Bulk update benchmark categories |
| `PATCH /api/models/{id}` | Update model metadata (param_count, is_pinned) |
| `DELETE /api/models/{id}` | Delete model + all data (cascading) |
| `DELETE /api/checkpoints/{id}` | Delete checkpoint + results (cascading) |

## Related

- [Eval360-V2](https://github.com/LLM360/Eval360-V2): Evaluation framework (scheduler + graders)
- [rl360-snapshots](https://github.com/LLM360/rl360-snapshots): RL training dashboard (shares EC2 + domain)
