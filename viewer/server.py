"""Eval360 Dashboard Viewer.

FastAPI server that serves the eval dashboard UI and provides
a query API over Postgres-backed eval results.

Usage:
    python server.py --port 11001
"""

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

async def lifespan(app: FastAPI):
    await db.init_pool()
    logger.info("Eval360 viewer server started")
    yield
    await db.close_pool()


app = FastAPI(title="Eval360 Dashboard Viewer", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_VIEWER_DIR = Path(__file__).parent


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_viewer():
    html = _VIEWER_DIR / "index.html"
    if html.exists():
        return FileResponse(html, media_type="text/html")
    return JSONResponse({"error": "index.html not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Ingest auth
# ---------------------------------------------------------------------------

INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")


async def verify_ingest_token(authorization: str = Header(...)):
    if not INGEST_TOKEN:
        raise HTTPException(503, "Ingest not configured")
    if authorization != f"Bearer {INGEST_TOKEN}":
        raise HTTPException(401, "Invalid ingest token")


# ---------------------------------------------------------------------------
# Ingest API
# ---------------------------------------------------------------------------

class IngestEvalResultPayload(BaseModel):
    model_id: str
    display_name: str
    model_type: str = "training"
    owner: str
    checkpoint_id: str
    training_step: int | None = None
    checkpoint_path: str | None = None
    dataset_name: str
    metrics: dict[str, float]
    primary_metric: str | None = None
    eval_config: dict = {}
    metadata: dict = {}


@app.post("/api/ingest/eval-result")
async def ingest_eval_result(body: IngestEvalResultPayload, _=Depends(verify_ingest_token)):
    # 1. Upsert model
    await db.upsert_model({
        "model_id": body.model_id,
        "display_name": body.display_name,
        "model_type": body.model_type,
        "owner": body.owner,
    })

    # 2. Upsert checkpoint
    await db.upsert_checkpoint({
        "checkpoint_id": body.checkpoint_id,
        "model_id": body.model_id,
        "training_step": body.training_step,
        "checkpoint_path": body.checkpoint_path,
        "metadata": body.metadata,
    })

    # 3. Upsert eval results
    primary = body.primary_metric or next(iter(body.metrics), None)
    for metric_name, metric_value in body.metrics.items():
        await db.upsert_eval_result({
            "checkpoint_id": body.checkpoint_id,
            "dataset_name": body.dataset_name,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "is_primary": metric_name == primary,
            "eval_config": body.eval_config,
        })

    return {"ok": True, "checkpoint_id": body.checkpoint_id, "dataset_name": body.dataset_name}


# ---------------------------------------------------------------------------
# Query API: Models
# ---------------------------------------------------------------------------

@app.get("/api/models")
async def list_models(
    model_type: str | None = Query(None),
    owner: str | None = Query(None),
):
    conditions = []
    params: list[Any] = []
    idx = 1

    if model_type:
        conditions.append(f"m.model_type = ${idx}")
        params.append(model_type)
        idx += 1
    if owner:
        conditions.append(f"m.owner = ${idx}")
        params.append(owner)
        idx += 1

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = await db.fetch(
        f"""
        SELECT m.*,
               COUNT(DISTINCT c.checkpoint_id) AS checkpoint_count,
               COUNT(DISTINCT e.dataset_name) AS dataset_count
        FROM models m
        LEFT JOIN checkpoints c ON c.model_id = m.model_id
        LEFT JOIN eval_results e ON e.checkpoint_id = c.checkpoint_id
        {where}
        GROUP BY m.model_id
        ORDER BY m.created_at DESC
        """,
        *params,
    )
    return {"models": [dict(r) for r in rows]}


@app.get("/api/models/{model_id}")
async def get_model(model_id: str):
    model = await db.fetchrow("SELECT * FROM models WHERE model_id = $1", model_id)
    if not model:
        return JSONResponse({"error": "Model not found"}, status_code=404)
    checkpoints = await db.fetch(
        "SELECT * FROM checkpoints WHERE model_id = $1 ORDER BY training_step ASC NULLS LAST",
        model_id,
    )
    return {"model": dict(model), "checkpoints": [dict(c) for c in checkpoints]}


@app.get("/api/models/{model_id}/scores")
async def get_model_scores(model_id: str):
    rows = await db.fetch(
        """
        SELECT e.*, c.training_step
        FROM eval_results e
        JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
        WHERE c.model_id = $1
        ORDER BY c.training_step ASC NULLS LAST, e.dataset_name, e.metric_name
        """,
        model_id,
    )
    return {"model_id": model_id, "scores": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Query API: Checkpoints
# ---------------------------------------------------------------------------

@app.get("/api/checkpoints/{checkpoint_id}")
async def get_checkpoint(checkpoint_id: str):
    checkpoint = await db.fetchrow("SELECT * FROM checkpoints WHERE checkpoint_id = $1", checkpoint_id)
    if not checkpoint:
        return JSONResponse({"error": "Checkpoint not found"}, status_code=404)
    results = await db.fetch(
        "SELECT * FROM eval_results WHERE checkpoint_id = $1 ORDER BY dataset_name, metric_name",
        checkpoint_id,
    )
    return {"checkpoint": dict(checkpoint), "eval_results": [dict(r) for r in results]}


# ---------------------------------------------------------------------------
# Query API: Datasets
# ---------------------------------------------------------------------------

@app.get("/api/datasets")
async def list_datasets():
    rows = await db.fetch("SELECT DISTINCT dataset_name FROM eval_results ORDER BY dataset_name")
    return {"datasets": [r["dataset_name"] for r in rows]}


@app.get("/api/datasets/{dataset_name}/leaderboard")
async def get_leaderboard(dataset_name: str):
    rows = await db.fetch(
        """
        SELECT m.model_id, m.display_name, m.model_type,
               e.metric_name, e.metric_value, e.checkpoint_id, c.training_step
        FROM eval_results e
        JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
        JOIN models m ON m.model_id = c.model_id
        WHERE e.dataset_name = $1 AND e.is_primary = TRUE
        AND e.metric_value = (
            SELECT MAX(e2.metric_value)
            FROM eval_results e2
            JOIN checkpoints c2 ON c2.checkpoint_id = e2.checkpoint_id
            WHERE c2.model_id = m.model_id
            AND e2.dataset_name = $1 AND e2.is_primary = TRUE
        )
        ORDER BY e.metric_value DESC
        """,
        dataset_name,
    )
    return {"dataset_name": dataset_name, "leaderboard": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Query API: Compare (multi-model overlay)
# ---------------------------------------------------------------------------

@app.get("/api/compare")
async def compare_models(
    models: str = Query(..., description="Comma-separated model_ids"),
    dataset: str = Query(..., description="Single dataset name"),
):
    model_ids = [m.strip() for m in models.split(",") if m.strip()]
    if not model_ids:
        return JSONResponse({"error": "No models specified"}, status_code=400)

    placeholders = ", ".join(f"${i+1}" for i in range(len(model_ids)))
    dataset_idx = len(model_ids) + 1

    rows = await db.fetch(
        f"""
        SELECT m.model_id, m.display_name, m.model_type,
               c.checkpoint_id, c.training_step,
               e.metric_name, e.metric_value
        FROM eval_results e
        JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
        JOIN models m ON m.model_id = c.model_id
        WHERE c.model_id IN ({placeholders})
        AND e.dataset_name = ${dataset_idx}
        AND e.is_primary = TRUE
        ORDER BY m.model_id, c.training_step ASC NULLS LAST
        """,
        *model_ids, dataset,
    )

    result: dict[str, Any] = {}
    for r in rows:
        mid = r["model_id"]
        if mid not in result:
            result[mid] = {
                "model_id": mid,
                "display_name": r["display_name"],
                "model_type": r["model_type"],
                "data_points": [],
            }
        result[mid]["data_points"].append({
            "checkpoint_id": r["checkpoint_id"],
            "training_step": r["training_step"],
            "metric_value": r["metric_value"],
        })

    return {"dataset": dataset, "models": list(result.values())}


# ---------------------------------------------------------------------------
# Filters (for dropdowns)
# ---------------------------------------------------------------------------

@app.get("/api/filters")
async def get_filter_options():
    owners = await db.fetch("SELECT DISTINCT owner FROM models ORDER BY owner")
    model_types = await db.fetch("SELECT DISTINCT model_type FROM models ORDER BY model_type")
    datasets = await db.fetch("SELECT DISTINCT dataset_name FROM eval_results ORDER BY dataset_name")
    return {
        "owners": [r["owner"] for r in owners],
        "model_types": [r["model_type"] for r in model_types],
        "datasets": [r["dataset_name"] for r in datasets],
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Eval360 Dashboard Viewer")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=11001)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
