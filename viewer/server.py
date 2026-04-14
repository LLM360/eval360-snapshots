"""Eval360 Dashboard Viewer.

FastAPI server that serves the eval dashboard UI and provides
a query API over Postgres-backed eval results.

Usage:
    python server.py --port 11001
"""

import argparse
import json
import logging
import math
import os
import uuid
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
    # Phase 1: provenance fields (all optional for backwards compatibility)
    eval_run_id: str | None = None
    harness_commit: str | None = None
    grader_type: str | None = None
    grader_version: str | None = None
    prompt_template: str | None = None
    inference_config: dict = {}
    dataset_version: str | None = None
    dataset_split: str = "test"
    sample_count: int | None = None
    seed: int | None = None
    status: str = "completed"
    error_message: str | None = None
    training_run: str | None = None
    recipe_tags: list[str] = []
    # Phase 2: confidence interval fields
    ci_lower: float | None = None
    ci_upper: float | None = None
    stderr: float | None = None
    # Phase 3: taxonomy fields
    category: str | None = None
    subcategory: str | None = None
    param_count: int | None = None


@app.post("/api/ingest/eval-result")
async def ingest_eval_result(body: IngestEvalResultPayload, _=Depends(verify_ingest_token)):
    # 1. Upsert model (now includes param_count)
    await db.upsert_model({
        "model_id": body.model_id,
        "display_name": body.display_name,
        "model_type": body.model_type,
        "owner": body.owner,
        "param_count": body.param_count,
    })

    # 1b. Upsert benchmark metadata if category provided
    if body.category:
        await db.upsert_benchmark_metadata({
            "dataset_name": body.dataset_name,
            "category": body.category,
            "subcategory": body.subcategory,
        })

    # 2. Upsert checkpoint (now includes training_run and recipe_tags)
    await db.upsert_checkpoint({
        "checkpoint_id": body.checkpoint_id,
        "model_id": body.model_id,
        "training_step": body.training_step,
        "checkpoint_path": body.checkpoint_path,
        "metadata": body.metadata,
        "training_run": body.training_run,
        "recipe_tags": body.recipe_tags,
    })

    # 3. Generate eval_run_id if not provided, then upsert eval_run
    eval_run_id = body.eval_run_id or str(uuid.uuid4())[:12]
    await db.upsert_eval_run({
        "eval_run_id": eval_run_id,
        "checkpoint_id": body.checkpoint_id,
        "dataset_name": body.dataset_name,
        "status": body.status,
        "harness_commit": body.harness_commit,
        "grader_type": body.grader_type,
        "grader_version": body.grader_version,
        "prompt_template": body.prompt_template,
        "inference_config": body.inference_config,
        "dataset_version": body.dataset_version,
        "dataset_split": body.dataset_split,
        "sample_count": body.sample_count,
        "seed": body.seed,
        "error_message": body.error_message,
    })

    # 4. Upsert eval results, linking each to the eval_run
    primary = body.primary_metric or next(iter(body.metrics), None)
    for metric_name, metric_value in body.metrics.items():
        ci_lo = body.ci_lower
        ci_hi = body.ci_upper
        se = body.stderr
        sample_n = body.sample_count if hasattr(body, 'sample_count') else None

        # Server-side CI fallback for proportion metrics
        if ci_lo is None and sample_n and 0 <= metric_value <= 1 and sample_n > 0:
            se = math.sqrt(metric_value * (1 - metric_value) / sample_n)
            ci_lo = max(0.0, round(metric_value - 1.96 * se, 6))
            ci_hi = min(1.0, round(metric_value + 1.96 * se, 6))
            se = round(se, 6)

        await db.upsert_eval_result({
            "checkpoint_id": body.checkpoint_id,
            "dataset_name": body.dataset_name,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "is_primary": metric_name == primary,
            "eval_config": body.eval_config,
            "eval_run_id": eval_run_id,
            "sample_count": body.sample_count,
            "ci_lower": ci_lo,
            "ci_upper": ci_hi,
            "stderr": se,
        })

    return {"ok": True, "checkpoint_id": body.checkpoint_id, "dataset_name": body.dataset_name, "eval_run_id": eval_run_id}


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
# Query API: Eval Runs
# ---------------------------------------------------------------------------

@app.get("/api/eval-runs/{eval_run_id}")
async def get_eval_run(eval_run_id: str):
    """Return full provenance for a single eval run."""
    row = await db.get_eval_run(eval_run_id)
    if not row:
        return JSONResponse({"error": "Eval run not found"}, status_code=404)
    return {"eval_run": dict(row)}


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
    common_only: bool = Query(False),
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

    if common_only and len(model_ids) > 1:
        # Filter to only data points at training steps where ALL models have results
        all_steps_per_model = {mid: {dp["training_step"] for dp in info["data_points"]} for mid, info in result.items()}
        common_steps = set.intersection(*all_steps_per_model.values()) if all_steps_per_model else set()
        for mid in result:
            result[mid]["data_points"] = [dp for dp in result[mid]["data_points"] if dp["training_step"] in common_steps]

    return {"dataset": dataset, "models": list(result.values())}


# ---------------------------------------------------------------------------
# Heatmap (all models × all datasets matrix)
# ---------------------------------------------------------------------------

async def _build_heatmap(dataset_filter: list[str] | None = None) -> dict:
    """Shared heatmap logic. If dataset_filter is provided, only include those datasets."""
    rows = await db.fetch(
        """
        WITH ranked AS (
            SELECT m.model_id, m.display_name, m.model_type, m.owner,
                   e.dataset_name, e.metric_value,
                   e.eval_run_id,
                   COALESCE(er.status, 'completed') AS run_status,
                   ROW_NUMBER() OVER (
                       PARTITION BY m.model_id, e.dataset_name
                       ORDER BY e.metric_value DESC NULLS LAST
                   ) AS rn
            FROM models m
            JOIN checkpoints c ON c.model_id = m.model_id
            JOIN eval_results e ON e.checkpoint_id = c.checkpoint_id
            LEFT JOIN eval_runs er ON er.eval_run_id = e.eval_run_id
            WHERE e.is_primary = TRUE
        )
        SELECT model_id, display_name, model_type, owner,
               dataset_name, metric_value AS best_score,
               eval_run_id, run_status
        FROM ranked
        WHERE rn = 1
        ORDER BY model_type DESC, model_id, dataset_name
        """
    )

    # Apply dataset filter if provided
    if dataset_filter is not None:
        filter_set = set(dataset_filter)
        rows = [r for r in rows if r["dataset_name"] in filter_set]

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
        matrix[mid][r["dataset_name"]] = {
            "score": round(r["best_score"], 4) if r["best_score"] is not None else None,
            "status": r["run_status"],
            "eval_run_id": r["eval_run_id"],
        }

    coverage = {}
    for m in models:
        mid = m["model_id"]
        evaluated = len([ds for ds in datasets if ds in matrix.get(mid, {}) and matrix[mid][ds].get("score") is not None])
        missing = [ds for ds in datasets if ds not in matrix.get(mid, {}) or matrix[mid][ds].get("score") is None]
        coverage[mid] = {"evaluated": evaluated, "total": len(datasets), "missing": missing}

    # Category grouping from benchmark_metadata
    cat_rows = await db.fetch("SELECT dataset_name, category FROM benchmark_metadata")
    categories: dict[str, list[str]] = {}
    for r in cat_rows:
        cat = r["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(r["dataset_name"])

    return {"models": models, "datasets": datasets, "matrix": matrix, "coverage": coverage, "categories": categories}


@app.get("/api/heatmap")
async def get_heatmap(suite_id: str | None = Query(None)):
    """Return best primary score per (model, dataset) with provenance for the heatmap matrix.

    Each cell is {score, status, eval_run_id} instead of a bare float.
    Optionally filter to a suite's datasets via ?suite_id=.
    """
    dataset_filter = None
    if suite_id:
        suite = await db.fetchrow("SELECT * FROM eval_suites WHERE suite_id = $1", suite_id)
        if not suite:
            return JSONResponse({"error": "Suite not found"}, status_code=404)
        dataset_filter = list(suite["dataset_names"])
    return await _build_heatmap(dataset_filter=dataset_filter)


# ---------------------------------------------------------------------------
# Phase 3: Suites + Benchmark Metadata + Model Metadata admin endpoints
# ---------------------------------------------------------------------------

class SuitePayload(BaseModel):
    suite_id: str
    display_name: str
    description: str | None = None
    dataset_names: list[str]


@app.post("/api/admin/suites")
async def create_suite(body: SuitePayload, _=Depends(verify_ingest_token)):
    await db.upsert_suite(body.model_dump())
    return {"ok": True, "suite_id": body.suite_id}


@app.get("/api/suites")
async def list_suites():
    rows = await db.fetch("SELECT * FROM eval_suites ORDER BY created_at")
    return {"suites": [dict(r) for r in rows]}


@app.get("/api/suites/{suite_id}/heatmap")
async def get_suite_heatmap(suite_id: str):
    suite = await db.fetchrow("SELECT * FROM eval_suites WHERE suite_id = $1", suite_id)
    if not suite:
        return JSONResponse({"error": "Suite not found"}, status_code=404)
    return await _build_heatmap(dataset_filter=list(suite["dataset_names"]))


class BenchmarkMetadataPayload(BaseModel):
    benchmarks: list[dict]


@app.post("/api/admin/benchmark-metadata")
async def update_benchmark_metadata(body: BenchmarkMetadataPayload, _=Depends(verify_ingest_token)):
    for bm in body.benchmarks:
        await db.upsert_benchmark_metadata(bm)
    return {"ok": True, "count": len(body.benchmarks)}


class ModelMetadataPayload(BaseModel):
    param_count: int | None = None
    is_pinned: bool | None = None


@app.patch("/api/models/{model_id}")
async def patch_model(model_id: str, body: ModelMetadataPayload, _=Depends(verify_ingest_token)):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return JSONResponse({"error": "No fields to update"}, status_code=400)
    await db.update_model_metadata(model_id, updates)
    return {"ok": True, "model_id": model_id}


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

    # Latest scores (now includes CI columns)
    latest_scores = await db.fetch(
        "SELECT dataset_name, metric_name, metric_value, ci_lower, ci_upper, stderr FROM eval_results "
        "WHERE checkpoint_id = $1 AND is_primary = TRUE",
        latest_cp["checkpoint_id"],
    )

    # Best baseline per dataset (with CI columns from the best-scoring row)
    baselines = await db.fetch(
        """
        WITH ranked_baselines AS (
            SELECT e.dataset_name, e.metric_value, e.ci_lower, e.ci_upper, e.stderr,
                   m.display_name,
                   ROW_NUMBER() OVER (PARTITION BY e.dataset_name ORDER BY e.metric_value DESC) AS rn
            FROM eval_results e
            JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
            JOIN models m ON m.model_id = c.model_id
            WHERE m.model_type = 'baseline' AND e.is_primary = TRUE
        )
        SELECT dataset_name, metric_value AS best_score, ci_lower, ci_upper, stderr, display_name AS best_model
        FROM ranked_baselines WHERE rn = 1
        """
    )
    baseline_map = {
        r["dataset_name"]: {
            "score": r["best_score"], "model": r["best_model"],
            "ci_lower": r["ci_lower"], "ci_upper": r["ci_upper"], "stderr": r["stderr"],
        } for r in baselines
    }

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

    def compute_significance(m_val, m_ci_lo, m_ci_hi, b_val, b_ci_lo, b_ci_hi):
        if m_ci_lo is None or b_ci_lo is None:
            return "insufficient_data"
        # Check for no overlap
        if m_ci_lo > b_ci_hi or b_ci_lo > m_ci_hi:
            return "likely_real"
        # Compute overlap fraction
        overlap = min(m_ci_hi, b_ci_hi) - max(m_ci_lo, b_ci_lo)
        m_width = m_ci_hi - m_ci_lo
        b_width = b_ci_hi - b_ci_lo
        smaller_width = min(m_width, b_width) if min(m_width, b_width) > 0 else 1
        if overlap / smaller_width > 0.5:
            return "likely_noise"
        return "uncertain"

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
        significance = compute_significance(
            val, s["ci_lower"], s["ci_upper"],
            bl_score, bl.get("ci_lower"), bl.get("ci_upper"),
        ) if bl_score is not None else "insufficient_data"
        scores[ds] = {
            "value": round(val, 4),
            "metric_name": s["metric_name"],
            "ci_lower": s["ci_lower"],
            "ci_upper": s["ci_upper"],
            "stderr": s["stderr"],
            "baseline_best": round(bl_score, 4) if bl_score else None,
            "baseline_model": bl.get("model"),
            "gap": gap,
            "significance": significance,
            "trend": compute_trend(recent_by_ds.get(ds, [])),
        }

    # Best overall checkpoint: highest average primary metric across all datasets
    best_overall_row = await db.fetchrow(
        """
        SELECT c.training_step
        FROM checkpoints c
        JOIN eval_results e ON e.checkpoint_id = c.checkpoint_id
        WHERE c.model_id = $1 AND e.is_primary = TRUE
        GROUP BY c.checkpoint_id, c.training_step
        ORDER BY AVG(e.metric_value) DESC
        LIMIT 1
        """,
        model_id,
    )
    best_overall_step = best_overall_row["training_step"] if best_overall_row else None

    # Best checkpoint per dataset: highest primary metric per dataset
    best_per_ds_rows = await db.fetch(
        """
        SELECT DISTINCT ON (e.dataset_name) e.dataset_name, c.training_step
        FROM eval_results e
        JOIN checkpoints c ON c.checkpoint_id = e.checkpoint_id
        WHERE c.model_id = $1 AND e.is_primary = TRUE
        ORDER BY e.dataset_name, e.metric_value DESC
        """,
        model_id,
    )
    best_per_dataset = {r["dataset_name"]: r["training_step"] for r in best_per_ds_rows}

    return {
        "model_id": model_id,
        "display_name": dict(model)["display_name"],
        "model_type": dict(model)["model_type"],
        "latest_checkpoint": latest_cp["checkpoint_id"],
        "latest_step": latest_cp["training_step"],
        "best_overall_step": best_overall_step,
        "best_per_dataset": best_per_dataset,
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
