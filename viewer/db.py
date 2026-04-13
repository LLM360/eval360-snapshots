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
        INSERT INTO checkpoints (checkpoint_id, model_id, training_step, checkpoint_path, metadata)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (checkpoint_id) DO UPDATE SET
            training_step = COALESCE(EXCLUDED.training_step, checkpoints.training_step),
            checkpoint_path = COALESCE(EXCLUDED.checkpoint_path, checkpoints.checkpoint_path),
            metadata = COALESCE(EXCLUDED.metadata, checkpoints.metadata)
        """,
        checkpoint["checkpoint_id"], checkpoint["model_id"],
        checkpoint.get("training_step"), checkpoint.get("checkpoint_path"),
        json.dumps(checkpoint.get("metadata", {})),
    )


async def upsert_eval_result(result: dict) -> None:
    await execute(
        """
        INSERT INTO eval_results (checkpoint_id, dataset_name, metric_name, metric_value, is_primary, eval_config)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (checkpoint_id, dataset_name, metric_name) DO UPDATE SET
            metric_value = EXCLUDED.metric_value,
            is_primary = EXCLUDED.is_primary,
            eval_config = EXCLUDED.eval_config,
            ingested_at = NOW()
        """,
        result["checkpoint_id"], result["dataset_name"],
        result["metric_name"], result["metric_value"],
        result.get("is_primary", False), json.dumps(result.get("eval_config", {})),
    )
