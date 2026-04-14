# Phase 4: Example-Level Drill-Down — Design Spec

**Date:** 2026-04-14
**Status:** Approved
**Issues:** #15, #16

## Summary

Add example-level result storage (metadata + previews in Postgres, full text on Weka) with a clickable example browser and slice analysis. Every aggregate score becomes explorable.

## Schema

```sql
CREATE TABLE IF NOT EXISTS example_results (
    id              SERIAL PRIMARY KEY,
    eval_run_id     TEXT NOT NULL REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
    example_idx     INTEGER NOT NULL,
    correct         BOOLEAN,
    input_preview   TEXT,
    output_preview  TEXT,
    ground_truth    TEXT,
    error_tag       TEXT,
    difficulty      TEXT,
    topic           TEXT,
    subtask         TEXT,
    metadata        JSONB DEFAULT '{}',
    UNIQUE (eval_run_id, example_idx)
);

CREATE INDEX IF NOT EXISTS idx_examples_run ON example_results(eval_run_id);
CREATE INDEX IF NOT EXISTS idx_examples_correct ON example_results(eval_run_id, correct);
CREATE INDEX IF NOT EXISTS idx_examples_topic ON example_results(topic);
CREATE INDEX IF NOT EXISTS idx_examples_difficulty ON example_results(difficulty);
```

No full text stored — input_preview and output_preview are first 200 chars. Full text stays on Weka as _grades.jsonl files.

## API

### POST /api/ingest/examples (requires ingest token)

Bulk ingest example results for an eval run.

```json
{
  "eval_run_id": "abc123",
  "examples": [
    {
      "example_idx": 0,
      "correct": true,
      "input_preview": "Solve: What is 2+2?",
      "output_preview": "The answer is 4.",
      "ground_truth": "4",
      "error_tag": null,
      "topic": "arithmetic",
      "difficulty": "easy",
      "subtask": null
    }
  ]
}
```

Uses INSERT ON CONFLICT DO UPDATE for idempotency.

### GET /api/eval-runs/{eval_run_id}/examples

Paginated example browser with filters.

Query params:
- `correct: bool | None` — filter by correctness
- `topic: str | None` — filter by topic
- `difficulty: str | None` — filter by difficulty
- `limit: int = 50`
- `offset: int = 0`

Returns:
```json
{
  "eval_run_id": "abc123",
  "total": 250,
  "correct_count": 203,
  "accuracy": 0.812,
  "examples": [...]
}
```

### GET /api/eval-runs/{eval_run_id}/slices

Aggregate breakdown by topic and difficulty.

Returns:
```json
{
  "eval_run_id": "abc123",
  "by_topic": {
    "algebra": {"total": 50, "correct": 42, "accuracy": 0.84},
    "geometry": {"total": 30, "correct": 18, "accuracy": 0.60}
  },
  "by_difficulty": {
    "easy": {"total": 80, "correct": 75, "accuracy": 0.9375},
    "hard": {"total": 40, "correct": 20, "accuracy": 0.50}
  },
  "error_concentration": "Failures concentrated in: geometry (12/30 failures), hard difficulty (20/40 failures)"
}
```

## Eval360-V2 Hook Changes

New model config field: `dashboard_logging_examples: bool = False`

When true, after posting scores, the hook:
1. Reads `_grades.jsonl` from the output path
2. For each graded example, extracts: correct (from the `correct` list), input (truncated to 200 chars), output (truncated), ground_truth, and any available metadata fields
3. POSTs to `/api/ingest/examples` in batches of 100

Added to ModelSpec and ModelInstance alongside existing `dashboard_logging`.

## Frontend

### Clickable score cells

Every completed score cell in the heatmap, gap table, and leaderboard becomes clickable. Clicking opens an example browser overlay/panel.

The click needs the `eval_run_id` (already available from heatmap data and provenance).

### Example browser panel

Opens as a full-width panel below the diagnostic grid (or as a modal). Contains:

**Header:**
- Dataset name + checkpoint
- Summary: "250 examples — 203 correct — 81.2% accuracy"
- Weka path (click to copy): derived from checkpoint_id + dataset_name

**Filter bar:**
- Toggle: All / Correct only / Incorrect only
- Topic dropdown (populated from slice data)
- Difficulty dropdown

**Table:**
- Columns: #, Input (preview), Output (preview), Expected, Correct, Topic, Difficulty
- Click row to expand: shows full ground_truth, metadata, error_tag
- Paginated (50 per page)

**Slice analysis (sidebar or below table):**
- Horizontal stacked bars per topic: green (correct) / red (incorrect)
- Same for difficulty
- Auto-summary: "Failures concentrated in: [top 2-3 failure slices]"

### CSS for example browser

```css
.example-browser { ... }
.example-filter-bar { ... }
.example-table { ... }
.example-expand { ... }
.slice-bars { ... }
```

## Mock Server Changes

- Add EXAMPLE_RESULTS list
- Seed ~20 examples per (training model latest checkpoint, dataset) with realistic topics/difficulties
- Support all 3 new endpoints
- Slice endpoint returns realistic breakdowns

## Files Changed

| File | Changes |
|------|---------|
| viewer/schema.sql | example_results table + indexes |
| viewer/db.py | bulk_insert_examples, query_examples, get_slices |
| viewer/server.py | 3 new endpoints |
| viewer/mock_server.py | seed examples, 3 endpoints |
| viewer/index.html | clickable cells, example browser, slice analysis, CSS |
| Eval360-V2/scheduler/dashboard_hook.py | example posting after scores |
| Eval360-V2/scheduler/model.py | dashboard_logging_examples field |
