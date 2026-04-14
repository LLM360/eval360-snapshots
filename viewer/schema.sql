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
