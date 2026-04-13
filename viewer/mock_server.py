"""Mock Eval360 Dashboard server for local demo.

Serves the real index.html with fake in-memory data. No Postgres required.
Generates realistic-looking eval results for training models and baselines.

Usage:
    pip install fastapi uvicorn pydantic
    python mock_server.py [--port 11001]
"""

import argparse
import logging
import math
import random
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mock data store
# ---------------------------------------------------------------------------

MODELS: list[dict] = []
CHECKPOINTS: list[dict] = []
EVAL_RESULTS: list[dict] = []

# Realistic model definitions
TRAINING_MODELS = [
    {"model_id": "k2-think-v2", "display_name": "K2-Think-V2", "owner": "varad", "steps": 15},
    {"model_id": "k2-chat-70b", "display_name": "K2-Chat-70B", "owner": "david", "steps": 10},
    {"model_id": "k2-base-400b", "display_name": "K2-Base-400B", "owner": "ming_shan", "steps": 8},
]

BASELINE_MODELS = [
    {"model_id": "qwen-2.5-72b", "display_name": "Qwen-2.5-72B", "owner": "external"},
    {"model_id": "claude-3.5-sonnet", "display_name": "Claude 3.5 Sonnet", "owner": "external"},
    {"model_id": "gpt-4o", "display_name": "GPT-4o", "owner": "external"},
]

DATASETS = {
    "bbh":        {"primary": "accuracy",                 "base": 0.55, "ceiling": 0.82},
    "math500":    {"primary": "accuracy",                 "base": 0.30, "ceiling": 0.75},
    "humaneval":  {"primary": "pass@1",                   "base": 0.40, "ceiling": 0.80},
    "ifeval":     {"primary": "strict_prompt_accuracy",   "base": 0.60, "ceiling": 0.93,
                   "extra": {"strict_instruction_accuracy": 0.02, "loose_prompt_accuracy": 0.03,
                             "loose_instruction_accuracy": 0.04}},
    "gsm8k":      {"primary": "accuracy",                 "base": 0.50, "ceiling": 0.88},
    "arc_challenge": {"primary": "accuracy",              "base": 0.55, "ceiling": 0.85},
    "mmlu_pro":   {"primary": "accuracy",                 "base": 0.45, "ceiling": 0.70},
    "mbpp":       {"primary": "pass@1",                   "base": 0.45, "ceiling": 0.78},
}

BASELINE_SCORES = {
    "qwen-2.5-72b":      {"bbh": 0.78, "math500": 0.68, "humaneval": 0.75, "ifeval": 0.88,
                           "gsm8k": 0.83, "arc_challenge": 0.80, "mmlu_pro": 0.65, "mbpp": 0.72},
    "claude-3.5-sonnet":  {"bbh": 0.83, "math500": 0.72, "humaneval": 0.82, "ifeval": 0.92,
                           "gsm8k": 0.90, "arc_challenge": 0.85, "mmlu_pro": 0.70, "mbpp": 0.78},
    "gpt-4o":            {"bbh": 0.80, "math500": 0.70, "humaneval": 0.80, "ifeval": 0.90,
                           "gsm8k": 0.87, "arc_challenge": 0.83, "mmlu_pro": 0.68, "mbpp": 0.75},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _training_score(base: float, ceiling: float, step: int, total_steps: int) -> float:
    """Generate a plausible training curve score."""
    frac = step / max(total_steps, 1)
    expected = base + (ceiling - base) * (1 - math.exp(-3 * frac))
    noise = random.gauss(0, 0.015)
    return max(0.0, min(1.0, round(expected + noise, 4)))


def seed_data():
    """Populate the mock data store with realistic eval results."""
    random.seed(42)

    for m in TRAINING_MODELS:
        MODELS.append({
            "model_id": m["model_id"], "display_name": m["display_name"],
            "model_type": "training", "owner": m["owner"], "created_at": _now(),
        })
        for step_idx in range(m["steps"]):
            step = (step_idx + 1) * 1000
            cp_id = f"{m['model_id']}__step-{step}"
            CHECKPOINTS.append({
                "checkpoint_id": cp_id, "model_id": m["model_id"],
                "training_step": step, "checkpoint_path": f"/checkpoints/{m['model_id']}/step-{step}",
                "metadata": {}, "created_at": _now(),
            })
            for ds_name, ds_info in DATASETS.items():
                primary_val = _training_score(ds_info["base"], ds_info["ceiling"], step_idx, m["steps"])
                EVAL_RESULTS.append({
                    "checkpoint_id": cp_id, "dataset_name": ds_name,
                    "metric_name": ds_info["primary"], "metric_value": primary_val,
                    "is_primary": True, "eval_config": {}, "ingested_at": _now(),
                })
                for extra_name, offset in ds_info.get("extra", {}).items():
                    EVAL_RESULTS.append({
                        "checkpoint_id": cp_id, "dataset_name": ds_name,
                        "metric_name": extra_name,
                        "metric_value": round(min(1.0, primary_val + offset + random.gauss(0, 0.005)), 4),
                        "is_primary": False, "eval_config": {}, "ingested_at": _now(),
                    })

    for m in BASELINE_MODELS:
        MODELS.append({
            "model_id": m["model_id"], "display_name": m["display_name"],
            "model_type": "baseline", "owner": m["owner"], "created_at": _now(),
        })
        cp_id = f"{m['model_id']}__baseline"
        CHECKPOINTS.append({
            "checkpoint_id": cp_id, "model_id": m["model_id"],
            "training_step": None, "checkpoint_path": None,
            "metadata": {}, "created_at": _now(),
        })
        for ds_name, score in BASELINE_SCORES[m["model_id"]].items():
            ds_info = DATASETS[ds_name]
            EVAL_RESULTS.append({
                "checkpoint_id": cp_id, "dataset_name": ds_name,
                "metric_name": ds_info["primary"], "metric_value": score,
                "is_primary": True, "eval_config": {}, "ingested_at": _now(),
            })


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Eval360 Dashboard Viewer (Mock)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_VIEWER_DIR = Path(__file__).parent


@app.get("/")
async def serve_viewer():
    html = _VIEWER_DIR / "index.html"
    if html.exists():
        return FileResponse(html, media_type="text/html")
    return JSONResponse({"error": "index.html not found"}, status_code=404)


@app.get("/api/models")
async def list_models(model_type: str | None = Query(None), owner: str | None = Query(None)):
    filtered = MODELS
    if model_type:
        filtered = [m for m in filtered if m["model_type"] == model_type]
    if owner:
        filtered = [m for m in filtered if m["owner"] == owner]
    result = []
    for m in filtered:
        cp_count = sum(1 for c in CHECKPOINTS if c["model_id"] == m["model_id"])
        ds_count = len({e["dataset_name"] for e in EVAL_RESULTS
                        if any(c["checkpoint_id"] == e["checkpoint_id"] and c["model_id"] == m["model_id"]
                               for c in CHECKPOINTS)})
        result.append({**m, "checkpoint_count": cp_count, "dataset_count": ds_count})
    return {"models": result}


@app.get("/api/models/{model_id}")
async def get_model(model_id: str):
    model = next((m for m in MODELS if m["model_id"] == model_id), None)
    if not model:
        return JSONResponse({"error": "Model not found"}, status_code=404)
    cps = sorted(
        [c for c in CHECKPOINTS if c["model_id"] == model_id],
        key=lambda c: c["training_step"] if c["training_step"] is not None else float("inf"),
    )
    return {"model": model, "checkpoints": cps}


@app.get("/api/models/{model_id}/scores")
async def get_model_scores(model_id: str):
    cp_ids = {c["checkpoint_id"] for c in CHECKPOINTS if c["model_id"] == model_id}
    cp_step = {c["checkpoint_id"]: c["training_step"] for c in CHECKPOINTS if c["model_id"] == model_id}
    results = [{**e, "training_step": cp_step.get(e["checkpoint_id"])}
               for e in EVAL_RESULTS if e["checkpoint_id"] in cp_ids]
    results.sort(key=lambda r: (r["training_step"] or float("inf"), r["dataset_name"], r["metric_name"]))
    return {"model_id": model_id, "scores": results}


@app.get("/api/checkpoints/{checkpoint_id}")
async def get_checkpoint(checkpoint_id: str):
    cp = next((c for c in CHECKPOINTS if c["checkpoint_id"] == checkpoint_id), None)
    if not cp:
        return JSONResponse({"error": "Checkpoint not found"}, status_code=404)
    return {"checkpoint": cp, "eval_results": [e for e in EVAL_RESULTS if e["checkpoint_id"] == checkpoint_id]}


@app.get("/api/datasets")
async def list_datasets():
    return {"datasets": sorted({e["dataset_name"] for e in EVAL_RESULTS})}


@app.get("/api/datasets/{dataset_name}/leaderboard")
async def get_leaderboard(dataset_name: str):
    best: dict[str, dict] = {}
    for e in EVAL_RESULTS:
        if e["dataset_name"] != dataset_name or not e["is_primary"]:
            continue
        cp = next(c for c in CHECKPOINTS if c["checkpoint_id"] == e["checkpoint_id"])
        mid = cp["model_id"]
        if mid not in best or e["metric_value"] > best[mid]["metric_value"]:
            model = next(m for m in MODELS if m["model_id"] == mid)
            best[mid] = {
                "model_id": mid, "display_name": model["display_name"],
                "model_type": model["model_type"], "metric_name": e["metric_name"],
                "metric_value": e["metric_value"], "checkpoint_id": e["checkpoint_id"],
                "training_step": cp["training_step"],
            }
    return {"dataset_name": dataset_name,
            "leaderboard": sorted(best.values(), key=lambda x: x["metric_value"], reverse=True)}


@app.get("/api/compare")
async def compare_models(
    models: str = Query(..., description="Comma-separated model_ids"),
    dataset: str = Query(..., description="Single dataset name"),
):
    model_ids = [m.strip() for m in models.split(",") if m.strip()]
    result: dict[str, dict] = {}
    for mid in model_ids:
        model = next((m for m in MODELS if m["model_id"] == mid), None)
        if not model:
            continue
        cp_ids = {c["checkpoint_id"]: c for c in CHECKPOINTS if c["model_id"] == mid}
        data_points = []
        for e in EVAL_RESULTS:
            if e["checkpoint_id"] in cp_ids and e["dataset_name"] == dataset and e["is_primary"]:
                cp = cp_ids[e["checkpoint_id"]]
                data_points.append({
                    "checkpoint_id": e["checkpoint_id"],
                    "training_step": cp["training_step"],
                    "metric_value": e["metric_value"],
                })
        data_points.sort(key=lambda d: d["training_step"] if d["training_step"] is not None else float("inf"))
        result[mid] = {
            "model_id": mid, "display_name": model["display_name"],
            "model_type": model["model_type"], "data_points": data_points,
        }
    return {"dataset": dataset, "models": list(result.values())}


@app.get("/api/filters")
async def get_filter_options():
    return {
        "owners": sorted({m["owner"] for m in MODELS}),
        "model_types": sorted({m["model_type"] for m in MODELS}),
        "datasets": sorted({e["dataset_name"] for e in EVAL_RESULTS}),
    }


def main():
    parser = argparse.ArgumentParser(description="Eval360 Dashboard Viewer (Mock)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=11001)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    seed_data()
    logger.info("Seeded %d models, %d checkpoints, %d eval results", len(MODELS), len(CHECKPOINTS), len(EVAL_RESULTS))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
