# Phase 1: Score Provenance + Eval Run Tracking — Design Spec

**Date:** 2026-04-14
**Status:** Approved
**Issues:** #8, #9, #10

## Summary

Make every eval result reproducible and disambiguated. Add eval run tracking with full provenance, replace the radar chart with a delta-to-reference bar chart, and add missing-state semantics to heatmap cells.

## Schema Changes

### New table: eval_runs

```sql
CREATE TABLE IF NOT EXISTS eval_runs (
    eval_run_id     TEXT PRIMARY KEY,
    checkpoint_id   TEXT NOT NULL REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
    dataset_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed'
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'invalidated')),
    harness_commit  TEXT,
    grader_type     TEXT,
    grader_version  TEXT,
    prompt_template TEXT,
    inference_config JSONB DEFAULT '{}',
    dataset_version TEXT,
    dataset_split   TEXT DEFAULT 'test',
    sample_count    INTEGER,
    seed            INTEGER,
    error_message   TEXT,
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (checkpoint_id, dataset_name, eval_run_id)
);
```

### Modifications to existing tables

```sql
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS eval_run_id TEXT REFERENCES eval_runs(eval_run_id);
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS sample_count INTEGER;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS training_run TEXT;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS recipe_tags TEXT[] DEFAULT '{}';
```

### Legacy data migration

After creating eval_runs, backfill synthetic rows for all existing results:

```sql
INSERT INTO eval_runs (eval_run_id, checkpoint_id, dataset_name, status, ingested_at)
SELECT
    'legacy-' || checkpoint_id || '-' || dataset_name,
    checkpoint_id,
    dataset_name,
    'completed',
    MIN(ingested_at)
FROM eval_results
WHERE eval_run_id IS NULL
GROUP BY checkpoint_id, dataset_name;

UPDATE eval_results SET eval_run_id = 'legacy-' || checkpoint_id || '-' || dataset_name
WHERE eval_run_id IS NULL;
```

### New indexes

```sql
CREATE INDEX IF NOT EXISTS idx_eval_runs_checkpoint ON eval_runs(checkpoint_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset ON eval_runs(dataset_name);
CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs(status);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(eval_run_id);
```

## Ingest API Changes

### Extended POST /api/ingest/eval-result payload

All new fields are optional — old payloads still work.

New fields:
- `eval_run_id: str | None` — auto-generated UUID if not provided
- `harness_commit: str | None`
- `grader_type: str | None`
- `grader_version: str | None`
- `prompt_template: str | None`
- `inference_config: dict = {}`
- `dataset_version: str | None`
- `dataset_split: str = "test"`
- `sample_count: int | None`
- `seed: int | None`
- `status: str = "completed"`
- `error_message: str | None`
- `training_run: str | None`
- `recipe_tags: list[str] = []`

Server behavior:
1. Upsert model (existing)
2. Upsert checkpoint — now also sets training_run and recipe_tags if provided
3. Upsert eval_run — creates row in eval_runs with provenance fields
4. Upsert eval_results — links each metric to the eval_run_id

### New endpoint: GET /api/eval-runs/{eval_run_id}

Returns full provenance for one eval run. Used by the frontend for hover tooltips (lazy-loaded, cached client-side).

### Eval360-V2 hook changes

`report_scores()` signature expands to accept all provenance fields explicitly. The caller in `scheduler.py` passes what it has: `task.grader.type`, `task.semantic_version`, `model_instance.openai_kwargs`, `task.num_generations`, etc.

## Frontend: Delta Bar Chart (Issue #9)

Replace the radar chart panel with a horizontal bar chart.

- Chart.js horizontal bar type
- X-axis: delta value (negative = behind, positive = ahead)
- Y-axis: benchmark names, sorted worst-first (most negative at top)
- Bars: red for negative gaps, green for positive
- Zero line clearly visible
- Reference selector: `<select>` above the chart, defaults to "Best baseline", lists all models
- Panel title: "Gap Analysis vs [Reference Model Name]"
- Gap table stays below as companion — updates when reference changes

## Frontend: Missing-State Semantics (Issue #10)

### Heatmap cell states

| State | Source | Display | Style |
|-------|--------|---------|-------|
| Completed | has score + status completed/legacy | `0.72` | HSL score color |
| Pending | status = pending or running | `⏳` | dim blue |
| Failed | status = failed | `✗` | dim red |
| Invalidated | status = invalidated | `⚠` | dim amber |
| Not run | no data | `—` | dim gray |

### Heatmap API response change

`/api/heatmap` matrix values change from bare float to object:

```json
{
  "math500": {"score": 0.72, "status": "completed", "eval_run_id": "abc123"},
  "bbh": {"score": null, "status": "pending", "eval_run_id": "def456"}
}
```

All consumers of the heatmap response (frontend observatory, leaderboard, models list score chips) must be updated.

### Provenance tooltip

Hovering a completed cell triggers a lazy fetch to `GET /api/eval-runs/{eval_run_id}`. Displays: dataset version, split, grader type + version, inference config, sample count, seed, timestamp, eval_run_id (click to copy). Results cached client-side.

Hovering a failed cell shows the error_message.

## Frontend: Checkpoint Disambiguation

### Model detail header strip

Below the model name, show:

```
Latest: step 5000  |  Best overall: step 4000  |  Best on math500: step 3000
```

- Latest: highest training_step
- Best overall: checkpoint with highest average primary metric across all datasets
- Best on [dataset]: dynamically updates on chart hover/click

### Checkpoint selector

Clicking a data point on any training curve selects that checkpoint. The gap table, delta bar chart, and provenance update to show the selected checkpoint's scores.

Default on page load: latest checkpoint.

## Mock Server

mock_server.py must be updated to:
- Store eval_runs in memory alongside MODELS/CHECKPOINTS/EVAL_RESULTS
- Return the new heatmap response shape (objects instead of bare floats)
- Support the new GET /api/eval-runs/{id} endpoint
- Generate a few pending/failed runs in seed data for testing missing states
- Support the reference selector on the delta bar chart

## Files Changed

| File | Changes |
|------|---------|
| `viewer/schema.sql` | Add eval_runs table, ALTER existing tables, migration SQL |
| `viewer/db.py` | Add upsert_eval_run(), get_eval_run() |
| `viewer/server.py` | Extend ingest payload, add eval-runs endpoint, update heatmap response |
| `viewer/mock_server.py` | Add eval_runs data, update heatmap shape, add eval-runs endpoint, seed pending/failed states |
| `viewer/index.html` | Replace radar with delta bars, add reference selector, update heatmap cells for 5 states, add provenance tooltips, add checkpoint strip + selector |
| `Eval360-V2/scheduler/dashboard_hook.py` | Expand report_scores() with provenance params |
| `Eval360-V2/scheduler/scheduler.py` | Pass provenance fields at all 3 call sites |
