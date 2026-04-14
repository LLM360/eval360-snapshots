"""Postgres connection pool and query helpers for the Eval360 dashboard."""

import json
import logging
import os
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool() -> asyncpg.Pool:
    global _pool
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL environment variable is required")
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    logger.info("Postgres pool created (%s)", dsn.split("@")[-1])
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    assert _pool is not None, "Call init_pool() first"
    return _pool


# ---------------------------------------------------------------------------
# Generic query helpers
# ---------------------------------------------------------------------------

async def fetch(query: str, *args: Any) -> list[asyncpg.Record]:
    return await pool().fetch(query, *args)


async def fetchrow(query: str, *args: Any) -> asyncpg.Record | None:
    return await pool().fetchrow(query, *args)


async def fetchval(query: str, *args: Any) -> Any:
    return await pool().fetchval(query, *args)


async def execute(query: str, *args: Any) -> str:
    return await pool().execute(query, *args)


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------

async def upsert_model(model: dict) -> None:
    await execute(
        """
        INSERT INTO models (model_id, display_name, model_type, owner)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (model_id) DO UPDATE SET
            display_name = EXCLUDED.display_name,
            owner = EXCLUDED.owner
        """,
        model["model_id"], model["display_name"],
        model.get("model_type", "training"), model["owner"],
    )


async def upsert_checkpoint(checkpoint: dict) -> None:
    await execute(
        """
        INSERT INTO checkpoints (checkpoint_id, model_id, training_step, checkpoint_path, metadata,
                                 training_run, recipe_tags)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (checkpoint_id) DO UPDATE SET
            training_step = COALESCE(EXCLUDED.training_step, checkpoints.training_step),
            checkpoint_path = COALESCE(EXCLUDED.checkpoint_path, checkpoints.checkpoint_path),
            metadata = COALESCE(EXCLUDED.metadata, checkpoints.metadata),
            training_run = COALESCE(EXCLUDED.training_run, checkpoints.training_run),
            recipe_tags = CASE WHEN EXCLUDED.recipe_tags = '{}' THEN checkpoints.recipe_tags
                               ELSE EXCLUDED.recipe_tags END
        """,
        checkpoint["checkpoint_id"], checkpoint["model_id"],
        checkpoint.get("training_step"), checkpoint.get("checkpoint_path"),
        json.dumps(checkpoint.get("metadata", {})),
        checkpoint.get("training_run"), checkpoint.get("recipe_tags", []),
    )


async def upsert_eval_result(result: dict) -> None:
    await execute(
        """
        INSERT INTO eval_results (checkpoint_id, dataset_name, metric_name, metric_value,
                                  is_primary, eval_config, eval_run_id, sample_count,
                                  ci_lower, ci_upper, stderr)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        ON CONFLICT (checkpoint_id, dataset_name, metric_name) DO UPDATE SET
            metric_value = EXCLUDED.metric_value,
            is_primary = EXCLUDED.is_primary,
            eval_config = EXCLUDED.eval_config,
            eval_run_id = COALESCE(EXCLUDED.eval_run_id, eval_results.eval_run_id),
            sample_count = COALESCE(EXCLUDED.sample_count, eval_results.sample_count),
            ci_lower = COALESCE(EXCLUDED.ci_lower, eval_results.ci_lower),
            ci_upper = COALESCE(EXCLUDED.ci_upper, eval_results.ci_upper),
            stderr = COALESCE(EXCLUDED.stderr, eval_results.stderr),
            ingested_at = NOW()
        """,
        result["checkpoint_id"], result["dataset_name"],
        result["metric_name"], result["metric_value"],
        result.get("is_primary", False), json.dumps(result.get("eval_config", {})),
        result.get("eval_run_id"), result.get("sample_count"),
        result.get("ci_lower"), result.get("ci_upper"), result.get("stderr"),
    )


async def upsert_eval_run(run: dict) -> None:
    await execute(
        """
        INSERT INTO eval_runs (eval_run_id, checkpoint_id, dataset_name, status,
                               harness_commit, grader_type, grader_version, prompt_template,
                               inference_config, dataset_version, dataset_split,
                               sample_count, seed, error_message)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        ON CONFLICT (eval_run_id) DO UPDATE SET
            status = EXCLUDED.status,
            harness_commit = COALESCE(EXCLUDED.harness_commit, eval_runs.harness_commit),
            grader_type = COALESCE(EXCLUDED.grader_type, eval_runs.grader_type),
            grader_version = COALESCE(EXCLUDED.grader_version, eval_runs.grader_version),
            prompt_template = COALESCE(EXCLUDED.prompt_template, eval_runs.prompt_template),
            inference_config = COALESCE(EXCLUDED.inference_config, eval_runs.inference_config),
            dataset_version = COALESCE(EXCLUDED.dataset_version, eval_runs.dataset_version),
            dataset_split = COALESCE(EXCLUDED.dataset_split, eval_runs.dataset_split),
            sample_count = COALESCE(EXCLUDED.sample_count, eval_runs.sample_count),
            seed = COALESCE(EXCLUDED.seed, eval_runs.seed),
            error_message = COALESCE(EXCLUDED.error_message, eval_runs.error_message)
        """,
        run["eval_run_id"], run["checkpoint_id"], run["dataset_name"],
        run.get("status", "completed"),
        run.get("harness_commit"), run.get("grader_type"), run.get("grader_version"),
        run.get("prompt_template"), json.dumps(run.get("inference_config", {})),
        run.get("dataset_version"), run.get("dataset_split", "test"),
        run.get("sample_count"), run.get("seed"), run.get("error_message"),
    )


async def get_eval_run(eval_run_id: str) -> asyncpg.Record | None:
    return await fetchrow("SELECT * FROM eval_runs WHERE eval_run_id = $1", eval_run_id)
