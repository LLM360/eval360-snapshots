# Phase 2: Statistical Rigor — Design Spec

**Date:** 2026-04-14
**Status:** Approved
**Issues:** #11, #12

## Summary

Add confidence intervals, significance indicators, and common-subset comparison to make dashboard comparisons trustworthy. Small deltas should be labeled as noise, not treated as real improvements.

## Schema Changes

```sql
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS ci_lower DOUBLE PRECISION;
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS ci_upper DOUBLE PRECISION;
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS stderr DOUBLE PRECISION;
```

## Ingest API Changes

Extend `IngestEvalResultPayload` with optional fields:
- `ci_lower: float | None`
- `ci_upper: float | None`
- `stderr: float | None`

**Server-side fallback:** If CI fields are not provided but `sample_count` is available and `0 <= metric_value <= 1`:
```python
se = math.sqrt(metric_value * (1 - metric_value) / sample_count)
ci_lower = max(0, metric_value - 1.96 * se)
ci_upper = min(1, metric_value + 1.96 * se)
stderr = se
```

Pass through to `db.upsert_eval_result()`.

## Diagnosis Endpoint Changes

Each score in `/api/models/{id}/diagnosis` gains:
- `ci_lower`, `ci_upper`, `stderr`
- `significance`: computed via CI overlap test against baseline

### CI Overlap Test

Given model CI `[m_lo, m_hi]` and baseline CI `[b_lo, b_hi]`:

1. Either side has no CI → `"insufficient_data"`
2. CIs don't overlap (`m_lo > b_hi` or `b_lo > m_hi`) → `"likely_real"`
3. Overlap > 50% of the smaller CI width → `"likely_noise"`
4. Otherwise → `"uncertain"`

## Common-Subset Comparison

### API: GET /api/compare

New optional param: `?common_only=true`

When set, filters to only benchmarks where ALL requested models have completed eval results.

### API: GET /api/heatmap

Add `coverage` field to response:
```json
{
  "coverage": {
    "k2-think-v2": {"evaluated": 7, "total": 8, "missing": ["aime-2024"]},
    "gpt-4o": {"evaluated": 4, "total": 8, "missing": [...]}
  }
}
```

`total` = number of datasets in the heatmap. `evaluated` = datasets with completed results. `missing` = dataset names without completed results.

## Frontend Changes

### Gap table significance column

New column after "Gap":

| Significance | Icon | Style |
|-------------|------|-------|
| `likely_real` | `●` | green if ahead, red if behind |
| `likely_noise` | `~` | dim gray |
| `uncertain` | `?` | dim amber |
| `insufficient_data` | `—` | dim gray |

Tooltip on hover explains the judgment.

### Delta bar chart

Bars for `likely_noise` gaps rendered at 40% opacity. `likely_real` gaps at full opacity. Visual distinction between real and noisy deltas.

### Coverage badges

- **Model cards**: `7/8` badge next to dataset count. Hover shows missing list.
- **Heatmap rows**: coverage fraction at row end.

### Common-only toggle

In compare controls (model detail page): checkbox "Common benchmarks only (5/8 shared)". When toggled, charts and gap table hide non-shared benchmarks. Hits `/api/compare?common_only=true`.

### Error bands on training curves

- Checkbox "Show uncertainty" above chart grid (default off)
- When on, adds shaded CI bands around each line
- Uses Chart.js fill between upper/lower bound datasets
- Band color: model color at 10% opacity

## Mock Server Changes

- Seed data generates `ci_lower`, `ci_upper`, `stderr` for all eval results (computed from sample_count)
- Some results deliberately have `sample_count=null` (no CIs) for testing `insufficient_data`
- Heatmap response includes `coverage` field
- Compare endpoint supports `common_only` param
- Diagnosis response includes CI fields and `significance`

## Files Changed

| File | Changes |
|------|---------|
| `viewer/schema.sql` | ALTER eval_results with CI columns |
| `viewer/db.py` | Extend upsert_eval_result with CI fields |
| `viewer/server.py` | CI fallback computation, significance in diagnosis, common_only on compare, coverage on heatmap |
| `viewer/mock_server.py` | CI seed data, coverage, common_only, significance |
| `viewer/index.html` | Significance column, bar opacity, coverage badges, common-only toggle, error bands |
