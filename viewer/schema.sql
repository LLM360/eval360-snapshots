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
