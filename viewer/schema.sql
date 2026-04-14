-- Eval360 Dashboard: Postgres schema
-- Run once: psql $DATABASE_URL -f schema.sql

-- Models: one row per model family
CREATE TABLE IF NOT EXISTS models (
    model_id        TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    model_type      TEXT NOT NULL DEFAULT 'training'
                    CHECK (model_type IN ('training', 'baseline')),
    owner           TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Checkpoints: one row per evaluable unit
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id   TEXT PRIMARY KEY,
    model_id        TEXT NOT NULL REFERENCES models(model_id) ON DELETE CASCADE,
    training_step   INTEGER,
    checkpoint_path TEXT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Eval results: one row per (checkpoint, dataset, metric)
CREATE TABLE IF NOT EXISTS eval_results (
    id              SERIAL PRIMARY KEY,
    checkpoint_id   TEXT NOT NULL REFERENCES checkpoints(checkpoint_id) ON DELETE CASCADE,
    dataset_name    TEXT NOT NULL,
    metric_name     TEXT NOT NULL,
    metric_value    DOUBLE PRECISION NOT NULL,
    is_primary      BOOLEAN DEFAULT FALSE,
    eval_config     JSONB DEFAULT '{}',
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (checkpoint_id, dataset_name, metric_name)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_eval_results_checkpoint ON eval_results(checkpoint_id);
CREATE INDEX IF NOT EXISTS idx_eval_results_dataset ON eval_results(dataset_name);
CREATE INDEX IF NOT EXISTS idx_eval_results_primary ON eval_results(dataset_name, is_primary)
    WHERE is_primary = TRUE;
CREATE INDEX IF NOT EXISTS idx_checkpoints_model ON checkpoints(model_id);
CREATE INDEX IF NOT EXISTS idx_checkpoints_step ON checkpoints(model_id, training_step);
CREATE INDEX IF NOT EXISTS idx_models_type ON models(model_type);
CREATE INDEX IF NOT EXISTS idx_models_owner ON models(owner);


-- =========================================================================
-- Phase 1: Score Provenance + Eval Run Tracking
-- =========================================================================

-- Eval runs: one row per evaluation execution with full provenance
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

-- Modifications to existing tables
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS eval_run_id TEXT REFERENCES eval_runs(eval_run_id);
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS sample_count INTEGER;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS training_run TEXT;
ALTER TABLE checkpoints ADD COLUMN IF NOT EXISTS recipe_tags TEXT[] DEFAULT '{}';

-- New indexes
CREATE INDEX IF NOT EXISTS idx_eval_runs_checkpoint ON eval_runs(checkpoint_id);
CREATE INDEX IF NOT EXISTS idx_eval_runs_dataset ON eval_runs(dataset_name);
CREATE INDEX IF NOT EXISTS idx_eval_runs_status ON eval_runs(status);
CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(eval_run_id);

-- Legacy data migration: backfill synthetic eval_run rows for existing results
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


-- =========================================================================
-- Phase 2: Confidence intervals
-- =========================================================================

ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS ci_lower DOUBLE PRECISION;
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS ci_upper DOUBLE PRECISION;
ALTER TABLE eval_results ADD COLUMN IF NOT EXISTS stderr DOUBLE PRECISION;


-- =========================================================================
-- Phase 3: Benchmark taxonomy + suites
-- =========================================================================

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


-- =========================================================================
-- Phase 4: Example-level results
-- =========================================================================

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


-- =========================================================================
-- Phase 5: Operational observatory + promotion gates
-- =========================================================================

-- Alerts: automated regression/improvement/promotion notifications
CREATE TABLE IF NOT EXISTS alerts (
    id              SERIAL PRIMARY KEY,
    alert_type      TEXT NOT NULL CHECK (alert_type IN ('regression', 'improvement', 'promotion_ready')),
    model_id        TEXT NOT NULL REFERENCES models(model_id) ON DELETE CASCADE,
    checkpoint_id   TEXT NOT NULL,
    dataset_name    TEXT,
    severity        TEXT NOT NULL DEFAULT 'info'
                    CHECK (severity IN ('critical', 'warning', 'info')),
    message         TEXT NOT NULL,
    detail          JSONB DEFAULT '{}',
    acknowledged    BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_alerts_model ON alerts(model_id);
CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type);
CREATE INDEX IF NOT EXISTS idx_alerts_unack ON alerts(acknowledged) WHERE acknowledged = FALSE;

-- Promotion rules: criteria for checkpoint readiness
CREATE TABLE IF NOT EXISTS promotion_rules (
    id              SERIAL PRIMARY KEY,
    rule_name       TEXT NOT NULL UNIQUE,
    model_id        TEXT,
    suite_id        TEXT,
    min_scores      JSONB DEFAULT '{}',
    no_regressions  BOOLEAN DEFAULT TRUE,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Activity log: audit trail of dashboard events
CREATE TABLE IF NOT EXISTS activity_log (
    id              SERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    model_id        TEXT,
    checkpoint_id   TEXT,
    dataset_name    TEXT,
    summary         TEXT NOT NULL,
    detail          JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_model ON activity_log(model_id);

-- Webhooks: optional HTTP callbacks on events
CREATE TABLE IF NOT EXISTS webhooks (
    id              SERIAL PRIMARY KEY,
    url             TEXT NOT NULL,
    events          TEXT[] NOT NULL,
    active          BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
