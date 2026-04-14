# Eval360: From Scoreboard to Evaluation Operating System

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform eval360-snapshots from a score display dashboard into a research-grade evaluation operating system that answers: what improved, what regressed, is it real, why, and should we promote this checkpoint.

**Architecture:** Phased evolution of the existing FastAPI + Postgres + single-file SPA. Each phase is independently deployable. Schema migrations are additive (new columns/tables, no breaking changes). Frontend grows by adding views, not rewriting existing ones.

**Tech Stack:** Python 3.12, FastAPI, asyncpg, Pydantic v2, Chart.js (CDN), Postgres

---

## Phasing Strategy

This spec contains 10 major improvement areas. They decompose into **5 phases**, each producing working software:

| Phase | Theme | Issues | Dependencies |
|-------|-------|--------|-------------|
| **1** | Score provenance + eval run tracking | #8, #9, #10 | None — foundational |
| **2** | Statistical rigor + missing-state semantics | #11, #12 | Phase 1 (needs sample_count) |
| **3** | Benchmark taxonomy + suites | #13, #14 | None — parallel with Phase 2 |
| **4** | Example-level drill-down | #15, #16 | Phase 1 (needs eval_run_id) |
| **5** | Operational observatory + promotion gates | #17, #18, #19 | Phases 1-3 |

**Each phase has its own set of GitHub issues below.** Phases 1 and 3 can run in parallel. Phases 2 and 4 depend on Phase 1. Phase 5 depends on all prior phases.

---

## Phase 1: Score Provenance + Eval Run Tracking

*Make every result reproducible and disambiguated.*

### GitHub Issue #8: Make checkpoint and eval run first-class objects in the schema

**Problem:** The current schema treats checkpoints as simple containers. There's no concept of an "eval run" — the provenance of a score (which harness version, which config, which seed) is stuffed into a freeform `eval_config` JSONB blob with no structure.

**Changes:**

**Schema migration** (`viewer/schema_v2.sql`):

```sql
-- Eval runs: one row per (checkpoint, dataset, config) evaluation execution
CREATE TABLE IF NOT EXISTS eval_runs (
    eval_run_id     TEXT PRIMARY KEY,
    checkpoint_id   TEXT NOT NULL REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
    dataset_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'completed'
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'invalidated')),
    -- Provenance fields
    harness_commit  TEXT,
    grader_type     TEXT,
    grader_version  TEXT,
    prompt_template TEXT,
    inference_config JSONB DEFAULT '{}',   -- temperature, max_tokens, seed, etc.
    dataset_version TEXT,                  -- semantic version or commit hash
    dataset_split   TEXT DEFAULT 'test',
    sample_count    INTEGER,
    seed            INTEGER,
    -- Timing
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (checkpoint_id, dataset_name, eval_run_id)
);

-- Migrate eval_results to reference eval_runs
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS eval_run_id TEXT REFERENCES eval_runs(eval_run_id);
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS sample_count INTEGER;

-- Add training_run and recipe to checkpoints
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS training_run TEXT;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS recipe_tags TEXT[] DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_eval_runs_checkpoint ON eval_runs(checkpoint_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset ON eval_runs(dataset_name);
CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs(status);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(eval_run_id);
```

**Ingest API changes** (`viewer/server.py`):

Extend `IngestEvalResultPayload` with optional provenance fields:

```python
class IngestEvalResultPayload(BaseModel):
    # ... existing fields ...
    # New provenance fields (all optional for backwards compat)
    eval_run_id: str | None = None          # auto-generated if not provided
    harness_commit: str | None = None
    grader_version: str | None = None
    prompt_template: str | None = None
    inference_config: dict = {}
    dataset_version: str | None = None
    dataset_split: str = "test"
    sample_count: int | None = None
    seed: int | None = None
    status: str = "completed"
```

**Frontend changes** (`viewer/index.html`):

- Score cells in heatmap/gap table get a tooltip showing provenance on hover
- Model detail header explicitly shows: "Selected: step 4000 | Latest: step 5000 | Best on MATH500: step 3000"
- Eval run status shown with distinct icons: completed ✓, pending ⏳, failed ✗, invalidated ⚠

**Acceptance criteria:**
- [ ] Schema migration runs without breaking existing data
- [ ] Ingest API accepts new provenance fields (old payloads still work)
- [ ] eval_runs table populated for new ingests
- [ ] Hovering a score cell shows provenance metadata
- [ ] Model detail shows selected/latest/best checkpoint distinction

---

### GitHub Issue #9: Replace radar chart with delta-to-baseline bar chart

**Problem:** Radar charts are visually appealing but scientifically weak — hard to compare precise differences, exaggerate shape, bad with sparse data. The gap table is already more informative.

**Changes:**

Replace the radar chart panel with a horizontal bar chart showing delta-to-baseline:
- X-axis: delta (negative = behind, positive = ahead)
- Y-axis: benchmark names
- Color: red for behind, green for ahead
- Zero line clearly marked
- Baseline model name shown in legend
- Sort: worst gaps at top (same as gap table)

Use Chart.js horizontal bar chart type. Keep the gap table as a companion — the bar chart is the visual, the table is the precise numbers.

**Acceptance criteria:**
- [ ] Radar chart replaced with horizontal delta bar chart
- [ ] Red/green color encoding for behind/ahead
- [ ] Sorted worst-first
- [ ] Gap table retained below the chart
- [ ] Works with the Compare overlay

---

### GitHub Issue #10: Add score provenance tooltips and missing-state semantics

**Problem:** A dash (`—`) in the heatmap doesn't distinguish between "not run", "pending", "failed", or "unsupported". Different states need different visual treatment.

**Changes:**

- Heatmap cells show distinct states: `—` (not run), `⏳` (pending), `✗` (failed), `⚠` (invalidated), value (completed)
- Each state gets a distinct muted color
- Hovering any completed score cell shows a tooltip with: dataset version, grader, inference config, sample count, timestamp, eval_run_id
- Hovering a failed cell shows the error message

**Acceptance criteria:**
- [ ] 5 distinct cell states rendered differently
- [ ] Provenance tooltip on completed cells
- [ ] Error tooltip on failed cells
- [ ] Status comes from eval_runs table (or inferred from eval_results for legacy data)

---

## Phase 2: Statistical Rigor

*Make comparisons trustworthy.*

### GitHub Issue #11: Add confidence intervals and significance indicators

**Problem:** Small deltas are over-interpreted. `0.7936 vs 0.8000` might be noise. The dashboard should quantify uncertainty.

**Changes:**

**Schema:**

```sql
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS ci_lower DOUBLE PRECISION;
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS ci_upper DOUBLE PRECISION;
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS stderr DOUBLE PRECISION;
```

**Ingest API:** Accept optional `ci_lower`, `ci_upper`, `stderr` fields. If not provided by the harness, compute bootstrap CI server-side from sample_count and metric_value (using the normal approximation for proportions: `±1.96 * sqrt(p*(1-p)/n)`).

**Frontend:**
- Gap table: add a "Significance" column showing "likely real" / "likely noise" / "insufficient data" based on whether CIs overlap
- Training curves: add optional error bands (shaded region around the line)
- Heatmap: cells with high uncertainty get a subtle "~" indicator

**Acceptance criteria:**
- [ ] CI fields stored in eval_results
- [ ] Server-side CI estimation when harness doesn't provide
- [ ] Gap table shows significance judgment
- [ ] Training curves support error bands toggle
- [ ] Sample size `n` shown in tooltips

---

### GitHub Issue #12: Add common-subset comparison toggle

**Problem:** If Model A is evaluated on 8 benchmarks and Model B on 5, aggregating over all benchmarks is misleading.

**Changes:**

- `/api/compare` endpoint accepts `?common_only=true` — only returns benchmarks where all requested models have results
- Frontend: toggle switch "Common benchmarks only" in the compare controls
- Coverage indicator on heatmap: e.g., "5/8 benchmarks evaluated"
- Coverage shown per model on the models list page

**Acceptance criteria:**
- [ ] API supports common_only filter
- [ ] Toggle in UI
- [ ] Coverage fraction shown on models page and heatmap

---

## Phase 3: Benchmark Taxonomy + Suites

*Structure the benchmark space.*

### GitHub Issue #13: Add benchmark categories and suite definitions

**Problem:** Flat benchmark lists don't scale. Researchers need to group benchmarks by capability area and define evaluation suites.

**Changes:**

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS benchmark_metadata (
    dataset_name    TEXT PRIMARY KEY,
    category        TEXT NOT NULL,       -- 'reasoning', 'coding', 'knowledge', 'safety', etc.
    subcategory     TEXT,                -- 'math', 'symbolic', 'pass_at_k', etc.
    primary_metric  TEXT,                -- default metric name for this benchmark
    description     TEXT
);

CREATE TABLE IF NOT EXISTS eval_suites (
    suite_id        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT,
    dataset_names   TEXT[] NOT NULL,     -- ordered list of benchmarks in this suite
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**Ingest API:** New endpoints:
- `POST /api/admin/benchmark-metadata` — register/update benchmark metadata
- `POST /api/admin/suites` — create/update suites
- `GET /api/suites` — list all suites
- `GET /api/suites/{suite_id}/heatmap` — heatmap filtered to suite benchmarks

**Frontend:**
- Heatmap: group columns by category with visual separators
- Suite selector dropdown on leaderboard and heatmap pages
- Category badges on benchmark names

**Acceptance criteria:**
- [ ] Benchmark metadata table with categories
- [ ] Suite definitions stored in DB
- [ ] Heatmap groupable by category
- [ ] Suite-filtered views
- [ ] Admin API for managing metadata and suites

---

### GitHub Issue #14: Add multi-reference comparison (pinned baselines + comparator types)

**Problem:** "Best baseline" is too coarse. A 4B model vs GPT-4o is aspirational but not always the right comparator.

**Changes:**

**Schema:**

```sql
ALTER TABLE models ADD COLUMN IF NOT EXISTS size_category TEXT;  -- '1B-7B', '7B-70B', '70B+'
ALTER TABLE models ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN DEFAULT FALSE;
```

**Frontend:**
- Compare controls: dropdown to select reference policy: "best external", "size-matched", "previous release", "custom"
- Gap table: reference column changes based on selected policy
- Pinned models marked with a 📌 icon in the models list

**Acceptance criteria:**
- [ ] Size category field on models
- [ ] Reference policy selector in compare controls
- [ ] Gap analysis adapts to selected reference
- [ ] Pinned models highlighted

---

## Phase 4: Example-Level Drill-Down

*Make the system diagnostic, not just reporting.*

### GitHub Issue #15: Add example-level result storage and API

**Problem:** The dashboard only stores aggregate scores. To diagnose why a benchmark is weak, you need to see individual examples, model outputs, and judge decisions.

**Changes:**

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS example_results (
    id              SERIAL PRIMARY KEY,
    eval_run_id     TEXT NOT NULL REFERENCES eval_runs(eval_run_id) ON DELETE CASCADE,
    example_idx     INTEGER NOT NULL,
    input_text      TEXT,
    ground_truth    TEXT,
    model_output    TEXT,
    parsed_output   TEXT,
    correct         BOOLEAN,
    judge_rationale TEXT,
    error_tag       TEXT,
    -- Slice labels
    difficulty      TEXT,
    topic           TEXT,
    subtask         TEXT,
    metadata        JSONB DEFAULT '{}',

    UNIQUE (eval_run_id, example_idx)
);

CREATE INDEX IF NOT EXISTS idx_examples_run ON example_results(eval_run_id);
CREATE INDEX IF NOT EXISTS idx_examples_correct ON example_results(eval_run_id, correct);
CREATE INDEX IF NOT EXISTS idx_examples_topic ON example_results(topic);
```

**Ingest API:**
- `POST /api/ingest/examples` — bulk ingest example results for an eval run (accepts JSONL body or array)
- `GET /api/eval-runs/{eval_run_id}/examples` — paginated example browser with filters

**Eval360-V2 hook update:** After grading, optionally POST the _grades.jsonl contents as example results (gated behind `dashboard_logging_examples: true` in model config to avoid overwhelming the DB).

**Acceptance criteria:**
- [ ] Example results table created
- [ ] Bulk ingest endpoint
- [ ] Paginated query endpoint with filters
- [ ] Eval360-V2 hook optionally sends example data

---

### GitHub Issue #16: Add example browser and slice analysis UI

**Problem:** Researchers need to see failing examples and analyze error patterns by slice (topic, difficulty, etc.).

**Changes:**

**Frontend — new view: Example Browser** (accessible by clicking a score cell):
- Table showing: example_idx, input (truncated), model output (truncated), expected, correct ✓/✗, topic, difficulty
- Filter by: correct/incorrect, topic, difficulty, error_tag
- Click row to expand full text
- Aggregate stats at top: "42/50 correct, 84%. Failures concentrated in: multi-step arithmetic (5/8 failures)"

**Frontend — slice analysis panel** on model detail:
- For each benchmark, show breakdown by topic/difficulty
- Horizontal stacked bar: correct vs incorrect per slice
- Highlight slices with lowest accuracy

**Acceptance criteria:**
- [ ] Score cells are clickable → opens example browser
- [ ] Example table with filtering
- [ ] Expandable row for full text
- [ ] Slice analysis bar chart
- [ ] Error concentration summary

---

## Phase 5: Operational Observatory

*Turn the dashboard from reporting into a decision-making surface.*

### GitHub Issue #17: Redesign Observatory as an operational monitoring page

**Problem:** Observatory and Leaderboard feel too similar. Observatory should answer: what changed, what regressed, what's pending.

**Changes:**

**Observatory redesign:**
- **Recent activity feed**: last N eval completions with delta indicators (↑↓→)
- **Regression alerts**: any checkpoint where latest score < previous score by > 2σ
- **Pending evaluations**: checkpoints that haven't been fully evaluated against the required suite
- **Coverage gaps**: benchmarks with stale or missing results
- **Failed runs**: eval runs in failed state

**This is NOT a heatmap.** It's a monitoring dashboard. The heatmap moves to the Leaderboard view (which becomes the analytical view).

**Acceptance criteria:**
- [ ] Activity feed showing recent ingestions with deltas
- [ ] Regression detection with significance
- [ ] Pending/incomplete evaluation list
- [ ] Failed runs list
- [ ] Clear visual separation from Leaderboard

---

### GitHub Issue #18: Add checkpoint promotion workflow

**Problem:** The dashboard shows scores but doesn't help with the decision: "should this checkpoint be promoted?"

**Changes:**

**Schema:**

```sql
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS promotion_status TEXT DEFAULT 'none'
    CHECK (promotion_status IN ('none', 'candidate', 'promoted', 'rejected'));
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS promotion_notes TEXT;
```

**Frontend:**
- Model detail: "Promotion Summary" strip showing best-overall, best-per-category, latest checkpoint
- Promote/Reject buttons (require ingest token)
- Promotion history log
- Simple rule display: "Passes core suite threshold (>0.75 avg)" / "Regression on safety suite"

**API:**
- `POST /api/checkpoints/{id}/promote` — mark as promoted
- `POST /api/checkpoints/{id}/reject` — mark as rejected with notes

**Acceptance criteria:**
- [ ] Promotion status field on checkpoints
- [ ] Promote/reject API endpoints
- [ ] Promotion summary strip in model detail
- [ ] Promotion status visible in models list

---

### GitHub Issue #19: Add models table view for scale + search/filter

**Problem:** Card layout breaks down at 20+ models. Need a table view with filtering.

**Changes:**

- Add a toggle: "Cards" / "Table" view on the models page
- Table columns: model name, type, owner, latest checkpoint, best checkpoint (on selected suite), coverage fraction, last eval time, promotion status
- Sortable by any column
- Search bar for model name
- Filter dropdowns for: type, owner, suite

**Acceptance criteria:**
- [ ] Table view toggle on models page
- [ ] Sortable columns
- [ ] Search bar
- [ ] Filter dropdowns
- [ ] Remembers view preference in localStorage

---

## Implementation Priority

**Start with Phase 1** — it's foundational and everything else depends on it. Issues #8, #9, #10.

Phase 3 (#13, #14) can run in parallel with Phase 2 (#11, #12) since they touch different parts of the system.

Phase 4 (#15, #16) is the highest-value diagnostic upgrade but requires Phase 1's eval_run_id.

Phase 5 (#17, #18, #19) ties everything together into an operational surface.

**Estimated effort per phase:**
- Phase 1: 2-3 days
- Phase 2: 1-2 days
- Phase 3: 2-3 days
- Phase 4: 3-5 days (largest — example storage + browser + slice analysis)
- Phase 5: 2-3 days
