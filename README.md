# eval360-snapshots

Dashboard for tracking LLM evaluation results across checkpoints and datasets. Shows score progression over training, radar capability profiles, gap analysis against baselines, and cross-model heatmaps.

## Architecture

```
Eval360-V2 scheduler ──POST scores──> eval360 viewer (FastAPI) ──> Postgres (RDS)
                                              │
Browser ──> Cloudflare ──> nginx ──> eval360 viewer
```

- **Ingest**: Eval360-V2 scheduler POSTs scores after grading (`--dashboard-logging`)
- **Backfill**: `backfill.py` CLI imports existing `_scores.yaml` files from past eval runs
- **Views**: Observatory heatmap, model detail (gap analysis, training curves, CIs), example browser, activity feed, leaderboard

The viewer shares an EC2 instance with the [rl360 viewer](https://github.com/LLM360/rl360-snapshots):

| Path | Service | Port |
|------|---------|------|
| `/rl360/` | RL360 training dashboard | 11001 |
| `/eval360/` | Eval360 eval dashboard | 11003 |

## Quick start (mock demo)

```bash
cd viewer
pip install fastapi uvicorn pydantic
python mock_server.py --port 11001
# Open http://localhost:11001
```

## Production deployment

See [viewer/README.md](viewer/README.md) for full setup instructions (venv, Postgres, systemd, nginx, Cloudflare).

## Ingesting scores from Eval360-V2

### Auto-ingest (recommended)

Enable dashboard logging via CLI flags when running eval:

```bash
eval360 --max-generation-jobs 1 --max-grading-parallelism 20 \
  evaluate-now \
  --model-paths model.yaml \
  --data-paths data.yaml \
  --dashboard-logging                  # log aggregate scores
  --dashboard-logging-examples         # also log per-example results (optional)
```

No env vars needed — the dashboard URL and token are read from a shared config file on Weka (`/mnt/weka/shrd/k2m/eval360/dashboard.env`).

To override: `export EVAL360_DASHBOARD_URL=... EVAL360_DASHBOARD_TOKEN=...`

### Backfill existing results

Import `_scores.yaml` files from past Eval360-V2 runs using `viewer/backfill.py`.

**Single model directory** (e.g., one checkpoint or one baseline):

```bash
# Training model — auto-extracts training step from directory name
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/k2-think-v2-step-5000/ \
  --model-id k2-think-v2 \
  --model-type training \
  --owner varad \
  --dashboard-url https://dashboard.llm360.ai/eval360 \
  --token <ingest-token>

# Baseline model
python viewer/backfill.py \
  --output-dir /path/to/Eval360-V2/output/gpt-4o/ \
  --model-id gpt-4o \
  --display-name "GPT-4o" \
  --model-type baseline \
  --owner external \
  --dashboard-url https://dashboard.llm360.ai/eval360 \
  --token <ingest-token>
```

**Bulk backfill** (multiple checkpoints under an output directory):

```bash
# Loop over all checkpoint directories for a training model
for dir in /path/to/Eval360-V2/output/k2-think-v2/step-*/; do
  python viewer/backfill.py \
    --output-dir "$dir" \
    --model-id k2-think-v2 \
    --model-type training \
    --owner varad \
    --dashboard-url https://dashboard.llm360.ai/eval360 \
    --token <ingest-token>
done

# Loop over all baseline models
for dir in /path/to/Eval360-V2/output/{gpt-4o,claude-3.5-sonnet,qwen-2.5-72b}/; do
  model_id=$(basename "$dir")
  python viewer/backfill.py \
    --output-dir "$dir" \
    --model-id "$model_id" \
    --model-type baseline \
    --owner external \
    --dashboard-url https://dashboard.llm360.ai/eval360 \
    --token <ingest-token>
done
```

The backfill script reads `*_scores.yaml` files, extracts training steps from directory names (e.g., `step-5000` → step 5000), and POSTs to the ingest API. It is idempotent — re-running it updates existing records.

**Backfill options:**

| Flag | Description |
|------|-------------|
| `--output-dir` | Directory containing `*_scores.yaml` files |
| `--model-id` | Model slug (e.g., `k2-think-v2`) |
| `--display-name` | Human-friendly name (default: model-id) |
| `--model-type` | `training` or `baseline` |
| `--owner` | Owner name (default: `backfill`) |
| `--training-step` | Override auto-detected step |
| `--checkpoint-id` | Override auto-generated checkpoint ID |
| `--primary-metric` | Which metric is primary (default: first key in YAML) |
| `--dashboard-url` | Dashboard base URL |
| `--token` | Ingest bearer token |

## API reference

### Query
| Endpoint | Description |
|----------|-------------|
| `GET /api/models` | List all models (filters: `model_type`, `owner`) |
| `GET /api/models/{id}` | Model detail + checkpoints |
| `GET /api/models/{id}/scores` | All eval results for a model |
| `GET /api/models/{id}/diagnosis` | Gap analysis, significance, trends, best checkpoints |
| `GET /api/models/{id}/promotion-status` | Promotion readiness check |
| `GET /api/checkpoints/{id}` | Checkpoint detail + results |
| `GET /api/datasets` | List dataset names |
| `GET /api/datasets/{name}/leaderboard` | Ranked by best primary metric |
| `GET /api/heatmap` | Models x datasets matrix with status + coverage + categories |
| `GET /api/compare` | Multi-model comparison (`?common_only=true`) |
| `GET /api/diff` | Compare two checkpoints (`?checkpoint_a=X&checkpoint_b=Y`) |
| `GET /api/alerts` | List alerts (filters: model_id, type, severity, acknowledged) |
| `GET /api/activity` | Activity feed (filters: model_id) |
| `GET /api/filters` | Distinct values for dropdowns |
| `GET /api/suites` | List evaluation suites |
| `GET /api/suites/{id}/heatmap` | Suite-filtered heatmap |
| `GET /api/eval-runs/{id}` | Eval run provenance details |
| `GET /api/eval-runs/{id}/examples` | Paginated examples (filters: correct, topic, difficulty) |
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
| `POST /api/admin/promotion-rules` | Create/update promotion rules |
| `POST /api/admin/webhooks` | Register webhook |
| `PATCH /api/models/{id}` | Update model metadata |
| `DELETE /api/models/{id}` | Delete model + all data (cascading) |
| `DELETE /api/checkpoints/{id}` | Delete checkpoint + results (cascading) |
| `POST /api/alerts/{id}/acknowledge` | Acknowledge an alert |

## Related

- [Eval360-V2](https://github.com/LLM360/Eval360-V2): Evaluation framework (scheduler + graders)
- [rl360-snapshots](https://github.com/LLM360/rl360-snapshots): RL training dashboard (shares EC2 + domain)
