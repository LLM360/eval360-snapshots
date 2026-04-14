# Backfill Guide

How to ingest existing Eval360-V2 results into the dashboard.

## Prerequisites

```bash
pip install httpx
```

You need the dashboard URL and ingest token from `/mnt/weka/shrd/k2m/eval360/dashboard.env`:

```bash
source /mnt/weka/shrd/k2m/eval360/dashboard.env
echo $EVAL360_DASHBOARD_URL   # https://dashboard.llm360.ai/eval360
echo $EVAL360_DASHBOARD_TOKEN
```

## Expected directory structure

The backfill script reads `*_scores.yaml` files from Eval360-V2 output directories:

```
output/
├── k2moe375b-mid1-checkpoint_0005000/
│   ├── aime-2026_generations.jsonl
│   ├── aime-2026_grades.jsonl
│   ├── aime-2026_scores.yaml          ← backfill reads this
│   ├── gpqa_diamond_scores.yaml
│   ├── ifeval_scores.yaml
│   └── mmlu_pro_scores.yaml
├── k2moe375b-mid1-checkpoint_0010000/
│   └── ...
└── gpt-4o/
    ├── bbh_scores.yaml
    └── math500_scores.yaml
```

Each `_scores.yaml` file contains lines like:

```yaml
"accuracy (avg over 32)": 0.8145833333333333
"bootstrap_std (avg over 32)": 0.05537456593133109
"accuracy (pass@1)": 0.8145833333333333
```

The first metric in the file is treated as the primary metric unless `--primary-metric` is specified.

## Training model (multiple checkpoints)

Training steps are auto-extracted from directory names (e.g., `checkpoint_0005000` → step 5000).

```bash
# Single checkpoint
python viewer/backfill.py \
  --output-dir /path/to/output/k2moe375b-mid1-checkpoint_0005000/ \
  --model-id k2moe375b-mid1 \
  --display-name "K2-MoE-375B Mid1" \
  --model-type training \
  --owner suqi.sun \
  --dashboard-url $EVAL360_DASHBOARD_URL \
  --token $EVAL360_DASHBOARD_TOKEN

# All checkpoints in a loop
for dir in /path/to/output/k2moe375b-mid1-checkpoint_*/; do
  echo "=== $(basename $dir) ==="
  python viewer/backfill.py \
    --output-dir "$dir" \
    --model-id k2moe375b-mid1 \
    --display-name "K2-MoE-375B Mid1" \
    --model-type training \
    --owner suqi.sun \
    --dashboard-url $EVAL360_DASHBOARD_URL \
    --token $EVAL360_DASHBOARD_TOKEN
done
```

## Baseline model (single checkpoint)

```bash
python viewer/backfill.py \
  --output-dir /path/to/output/gpt-4o/ \
  --model-id gpt-4o \
  --display-name "GPT-4o" \
  --model-type baseline \
  --owner external \
  --dashboard-url $EVAL360_DASHBOARD_URL \
  --token $EVAL360_DASHBOARD_TOKEN
```

## Bulk backfill (multiple models)

```bash
# Multiple training models under a shared results directory
RESULTS_DIR=/mnt/weka/shrd/k2m/suqi.sun/moe-mid1-results

# Mid1: 6 checkpoints
for dir in $RESULTS_DIR/k2moe375b-mid1-checkpoint_*/; do
  python viewer/backfill.py \
    --output-dir "$dir" \
    --model-id k2moe375b-mid1 \
    --display-name "K2-MoE-375B Mid1" \
    --model-type training \
    --owner suqi.sun \
    --dashboard-url $EVAL360_DASHBOARD_URL \
    --token $EVAL360_DASHBOARD_TOKEN
done

# Mid2: 2 checkpoints
for dir in $RESULTS_DIR/k2moe375b-mid2-checkpoint_*/; do
  python viewer/backfill.py \
    --output-dir "$dir" \
    --model-id k2moe375b-mid2 \
    --display-name "K2-MoE-375B Mid2" \
    --model-type training \
    --owner suqi.sun \
    --dashboard-url $EVAL360_DASHBOARD_URL \
    --token $EVAL360_DASHBOARD_TOKEN
done

# All baselines
for dir in /path/to/output/{gpt-4o,claude-3.5-sonnet,qwen-2.5-72b}/; do
  model_id=$(basename "$dir")
  python viewer/backfill.py \
    --output-dir "$dir" \
    --model-id "$model_id" \
    --model-type baseline \
    --owner external \
    --dashboard-url $EVAL360_DASHBOARD_URL \
    --token $EVAL360_DASHBOARD_TOKEN
done
```

## CLI reference

| Flag | Required | Description |
|------|----------|-------------|
| `--output-dir` | Yes | Directory containing `*_scores.yaml` files |
| `--model-id` | Yes | Model slug (e.g., `k2moe375b-mid1`) |
| `--model-type` | Yes | `training` or `baseline` |
| `--dashboard-url` | Yes | Dashboard base URL |
| `--token` | Yes | Ingest bearer token |
| `--display-name` | No | Human-friendly name (default: model-id) |
| `--owner` | No | Owner name (default: `backfill`) |
| `--training-step` | No | Override auto-detected step from directory name |
| `--checkpoint-id` | No | Override auto-generated checkpoint ID |
| `--primary-metric` | No | Which metric is primary (default: first key in YAML) |

## Notes

- The script is **idempotent** — re-running updates existing records rather than duplicating them.
- Training steps are extracted from the directory name by matching trailing integers: `checkpoint_0005000` → 5000, `step-100` → 100.
- For baseline models, no training step is set and the checkpoint ID defaults to `<model-id>__baseline`.
- The first metric key in each `_scores.yaml` file is used as the primary metric unless overridden.
- CI (confidence intervals) are computed server-side from the metric value and sample count when not provided explicitly.
