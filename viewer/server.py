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


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str, _=Depends(verify_ingest_token)):
    """Delete a model and all its checkpoints and eval results (cascading)."""
    row = await db.fetchrow("SELECT model_id FROM models WHERE model_id = $1", model_id)
    if not row:
        return JSONResponse({"error": "Model not found"}, status_code=404)
    await db.execute("DELETE FROM models WHERE model_id = $1", model_id)
    return {"ok": True, "deleted": model_id}


@app.delete("/api/checkpoints/{checkpoint_id}")
async def delete_checkpoint(checkpoint_id: str, _=Depends(verify_ingest_token)):
    """Delete a checkpoint and its eval results (cascading)."""
    row = await db.fetchrow("SELECT checkpoint_id FROM checkpoints WHERE checkpoint_id = $1", checkpoint_id)
    if not row:
        return JSONResponse({"error": "Checkpoint not found"}, status_code=404)
    await db.execute("DELETE FROM checkpoints WHERE checkpoint_id = $1", checkpoint_id)
    return {"ok": True, "deleted": checkpoint_id}


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
# Heatmap (all models × all datasets matrix)
# ---------------------------------------------------------------------------

@app.get("/api/heatmap")
async def get_heatmap():
    """Return best primary score per (model, dataset) for the heatmap matrix."""
    rows = await db.fetch(
        """
        SELECT m.model_id, m.display_name, m.model_type, m.owner,
               e.dataset_name, MAX(e.metric_value) AS best_score
        FROM models m
        JOIN checkpoints c ON c.model_id = m.model_id
        JOIN eval_results e ON e.checkpoint_id = c.checkpoint_id
        WHERE e.is_primary = TRUE
        GROUP BY m.model_id, m.display_name, m.model_type, m.owner, e.dataset_name
        ORDER BY m.model_type DESC, m.model_id, e.dataset_name
        """
    )

    datasets = sorted({r["dataset_name"] for r in rows})
    models = []
    matrix: dict[str, dict] = {}
    seen = set()

    for r in rows:
        mid = r["model_id"]
        if mid not in seen:
            seen.add(mid)
            models.append({
                "model_id": mid, "display_name": r["display_name"],
                "model_type": r["model_type"], "owner": r["owner"],
            })
        if mid not in matrix:
            matrix[mid] = {}
        matrix[mid][r["dataset_name"]] = round(r["best_score"], 4)

    return {"models": models, "datasets": datasets, "matrix": matrix}


# ---------------------------------------------------------------------------
# Model diagnosis (gap analysis + trends)
# ---------------------------------------------------------------------------

@app.get("/api/models/{model_id}/diagnosis")
async def get_model_diagnosis(model_id: str):
    """Return latest scores, baseline gaps, and trend indicators for a model."""
    model = await db.fetchrow("SELECT * FROM models WHERE model_id = $1", model_id)
    if not model:
        return JSONResponse({"error": "Model not found"}, status_code=404)

    # Latest checkpoint
    latest_cp = await db.fetchrow(
        "SELECT checkpoint_id, training_step FROM checkpoints "
        "WHERE model_id = $1 ORDER BY training_step DESC NULLS LAST LIMIT 1",
        model_id,
    )
    if not latest_cp:
        return {"model_id": model_id, "scores": {}, "latest_checkpoint": None}

    # Latest scores
    latest_scores = await db.fetch(
        "SELECT dataset_name, metric_name, metric_value FROM eval_results "
        "WHERE checkpoint_id = $1 AND is_primary = TRUE",
        latest_cp["checkpoint_id"],
    )

    # Best baseline per dataset
    baselines = await db.fetch(
        """
        SELECT e.dataset_name, MAX(e.metric_value) AS best_score,
               (array_agg(m.display_name ORDER BY e.metric_value DESC))[1] AS best_model
        FROM eval_results e
        JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
        JOIN models m ON m.model_id = c.model_id
        WHERE m.model_type = 'baseline' AND e.is_primary = TRUE
        GROUP BY e.dataset_name
        """
    )
    baseline_map = {r["dataset_name"]: {"score": r["best_score"], "model": r["best_model"]} for r in baselines}

    # Recent checkpoints for trend (last 5)
    recent = await db.fetch(
        """
        SELECT c.training_step, e.dataset_name, e.metric_value
        FROM eval_results e
        JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
        WHERE c.model_id = $1 AND e.is_primary = TRUE
        ORDER BY c.training_step DESC NULLS LAST
        """,
        model_id,
    )

    # Group recent by dataset, compute trend
    recent_by_ds: dict[str, list] = {}
    for r in recent:
        ds = r["dataset_name"]
        if ds not in recent_by_ds:
            recent_by_ds[ds] = []
        if len(recent_by_ds[ds]) < 5:
            recent_by_ds[ds].append(r["metric_value"])

    def compute_trend(values: list) -> str:
        if len(values) < 2:
            return "new"
        # values are newest-first; compare latest vs 2nd-latest
        delta = values[0] - values[1]
        if delta > 0.02:
            return "up"
        if delta < -0.02:
            return "down"
        # Check longer trend if available
        if len(values) >= 3:
            long_delta = values[0] - values[-1]
            if long_delta > 0.03:
                return "up_slow"
            if long_delta < -0.03:
                return "down_slow"
        return "flat"

    scores = {}
    for s in latest_scores:
        ds = s["dataset_name"]
        bl = baseline_map.get(ds, {})
        val = s["metric_value"]
        bl_score = bl.get("score")
        gap = round(val - bl_score, 4) if bl_score is not None else None
        scores[ds] = {
            "value": round(val, 4),
            "metric_name": s["metric_name"],
            "baseline_best": round(bl_score, 4) if bl_score else None,
            "baseline_model": bl.get("model"),
            "gap": gap,
            "trend": compute_trend(recent_by_ds.get(ds, [])),
        }

    return {
        "model_id": model_id,
        "display_name": dict(model)["display_name"],
        "model_type": dict(model)["model_type"],
        "latest_checkpoint": latest_cp["checkpoint_id"],
        "latest_step": latest_cp["training_step"],
        "scores": scores,
    }


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
