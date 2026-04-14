# Phase 3: Benchmark Taxonomy + Suites — Design Spec

**Date:** 2026-04-14
**Status:** Approved
**Issues:** #13, #14

## Summary

Add benchmark categories, evaluation suites, model size metadata, and multi-reference comparison. Categories flow from Eval360-V2 dataset configs. Suites are managed via admin API.

## Schema Changes

```sql
CREATE TABLE IF NOT EXISTS benchmark_metadata (
    dataset_name    TEXT PRIMARY KEY,
    category        TEXT NOT NULL DEFAULT 'uncategorized',
    subcategory     TEXT,
    primary_metric  TEXT,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS eval_suites (
    suite_id        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT,
    dataset_names   TEXT[] NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE models ADD COLUMN IF NOT EXISTS param_count BIGINT;
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN DEFAULT FALSE;
```

## Ingest API Changes

Extend `IngestEvalResultPayload` with optional fields:
- `category: str | None` — benchmark category (reasoning, coding, knowledge, safety, etc.)
- `subcategory: str | None` — e.g., math, symbolic, pass_at_k
- `param_count: int | None` — model parameter count

If `category` is provided, upsert into `benchmark_metadata`. If `param_count` is provided, update the models row.

## Eval360-V2 Hook Changes

Read `task.meta.category` and `task.meta.subcategory` from the dataset config if present, pass through to the ingest payload. No changes needed to dataset YAMLs until users add `category` to their configs.

## Admin API

All admin endpoints require ingest token auth.

### POST /api/admin/suites
```json
{"suite_id": "core", "display_name": "Core Research Suite", "description": "Primary benchmarks", "dataset_names": ["bbh", "math500", "humaneval", "gsm8k"]}
```
Upserts a suite.

### GET /api/suites
Returns all suites.

### GET /api/suites/{suite_id}/heatmap
Same as `/api/heatmap` but filtered to the suite's datasets.

### POST /api/admin/benchmark-metadata
Bulk upsert: `{"benchmarks": [{"dataset_name": "bbh", "category": "reasoning", "subcategory": "logic", "primary_metric": "accuracy"}]}`

### PATCH /api/models/{model_id}
Update model metadata: `{"param_count": 4000000000, "is_pinned": true}`. Requires ingest token.

## Heatmap Changes

### Suite filter
`GET /api/heatmap` accepts optional `?suite_id=core`. Filters datasets to the suite's list.

### Category grouping
Response includes `categories` field:
```json
{
  "categories": {
    "reasoning": ["bbh", "gsm8k", "math500"],
    "coding": ["humaneval", "mbpp"],
    "knowledge": ["arc_challenge", "mmlu_pro"],
    "instruction_following": ["ifeval"]
  }
}
```
Frontend renders column headers grouped by category with visual separators.

## Frontend Changes

### Heatmap
- Suite selector dropdown above heatmap (fetches from `/api/suites`)
- Column headers grouped by category with border separators
- Category labels as sub-headers

### Model cards
- Pinned models: 📌 icon in header
- Param count: "4B" badge if available
- Existing coverage badge stays

### Reference selector (model detail)
Expanded options:
- "Best baseline" (default)
- "Size-matched (±50%)" — filters models by param_count within ±50% of primary model, picks best
- Individual model list (all models, regardless of size)

Size matching is a frontend filter — no backend change needed beyond returning param_count in the models API.

### Leaderboard
- Suite selector dropdown filters the pivot table
- Reuses the same `/api/heatmap?suite_id=X` endpoint

## Mock Server Changes

- Add BENCHMARK_METADATA and EVAL_SUITES stores
- Seed 2-3 suites (Core, Coding, Full)
- Seed categories for all datasets
- Seed param_count for models (4B, 70B, 400B, null for externals)
- Support suite_id filter on heatmap
- Support admin endpoints (in-memory)
- Return categories grouping in heatmap response

## Files Changed

| File | Changes |
|------|---------|
| `viewer/schema.sql` | benchmark_metadata + eval_suites tables, ALTER models |
| `viewer/db.py` | upsert_benchmark_metadata, upsert_suite, get_suites, update_model_metadata |
| `viewer/server.py` | Admin endpoints, heatmap suite filter, category grouping, model PATCH, ingest category pass-through |
| `viewer/mock_server.py` | Seed suites/categories/param_count, admin endpoints, suite filter |
| `viewer/index.html` | Suite selector, category-grouped heatmap, pinned/size badges, expanded reference selector |
| `Eval360-V2/scheduler/dashboard_hook.py` | Pass category/subcategory from task.meta |
