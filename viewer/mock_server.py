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
EVAL_RUNS: list[dict] = []
BENCHMARK_METADATA: list[dict] = []
EVAL_SUITES: list[dict] = []

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


# Grader type per dataset (used in seed eval_runs)
_DATASET_GRADER = {
    "bbh": "exact_match",
    "math500": "math_verify",
    "humaneval": "execution",
    "ifeval": "rule_based",
    "gsm8k": "exact_match",
    "arc_challenge": "exact_match",
    "mmlu_pro": "exact_match",
    "mbpp": "execution",
}

# Sample count per dataset
_DATASET_SAMPLES = {
    "aime": 30,
    "bbh": 250,
    "math500": 500,
    "humaneval": 164,
    "ifeval": 541,
    "gsm8k": 1319,
    "arc_challenge": 1172,
    "mmlu_pro": 1000,
    "mbpp": 378,
}


def _compute_ci(primary_val: float, sample_n: int | None) -> tuple:
    """Return (ci_lower, ci_upper, stderr, sample_count) for a proportion metric.

    Returns (None, None, None, None) when sample_n is None (insufficient data).
    """
    if sample_n is None:
        return None, None, None, None
    if sample_n > 0 and 0 < primary_val < 1:
        se = math.sqrt(primary_val * (1 - primary_val) / sample_n)
    else:
        se = 0.0
    ci_lower = max(0.0, round(primary_val - 1.96 * se, 6))
    ci_upper = min(1.0, round(primary_val + 1.96 * se, 6))
    return ci_lower, ci_upper, round(se, 6), sample_n


def seed_data():
    """Populate the mock data store with realistic eval results."""
    random.seed(42)

    # Track which (cp_id, ds_name) pairs will be pending/failed instead of completed
    # We'll assign these after building the full list of training combos
    pending_runs: set[tuple[str, str]] = set()
    failed_runs: dict[tuple[str, str], str] = {}

    # We need a stable selection; pick after the loop by recording all combos first
    training_combos: list[tuple[str, str]] = []

    _PARAM_COUNTS = {"k2-think-v2": 400_000_000_000, "k2-chat-70b": 70_000_000_000, "k2-base-400b": 400_000_000_000}

    for m in TRAINING_MODELS:
        MODELS.append({
            "model_id": m["model_id"], "display_name": m["display_name"],
            "model_type": "training", "owner": m["owner"], "created_at": _now(),
            "param_count": _PARAM_COUNTS.get(m["model_id"]),
            "is_pinned": False,
        })
        for step_idx in range(m["steps"]):
            step = (step_idx + 1) * 1000
            cp_id = f"{m['model_id']}__step-{step}"
            CHECKPOINTS.append({
                "checkpoint_id": cp_id, "model_id": m["model_id"],
                "training_step": step, "checkpoint_path": f"/checkpoints/{m['model_id']}/step-{step}",
                "metadata": {}, "created_at": _now(),
            })
            for ds_name in DATASETS:
                training_combos.append((cp_id, ds_name))

    # Choose 2 pending and 1 failed from later-step checkpoints (not step-1000 to keep early
    # steps fully populated for a cleaner training curve).
    rng = random.Random(99)
    eligible = [(cp, ds) for cp, ds in training_combos if "step-5000" in cp or "step-4000" in cp]
    chosen = rng.sample(eligible, min(3, len(eligible)))
    pending_runs = {chosen[0], chosen[1]}
    failed_runs = {chosen[2]: "OOM during grading: CUDA out of memory on rank 0"}

    # Now emit EVAL_RESULTS and EVAL_RUNS for training models
    # Re-seed so the score values are identical to the original seeding order
    random.seed(42)
    for m in TRAINING_MODELS:
        for step_idx in range(m["steps"]):
            step = (step_idx + 1) * 1000
            cp_id = f"{m['model_id']}__step-{step}"
            for ds_name, ds_info in DATASETS.items():
                primary_val = _training_score(ds_info["base"], ds_info["ceiling"], step_idx, m["steps"])
                key = (cp_id, ds_name)
                run_id = f"{cp_id}__{ds_name}"

                if key in pending_runs:
                    # pending: eval_run exists but no eval_result
                    EVAL_RUNS.append({
                        "eval_run_id": run_id,
                        "checkpoint_id": cp_id,
                        "dataset_name": ds_name,
                        "status": "pending",
                        "grader_type": _DATASET_GRADER.get(ds_name, "exact_match"),
                        "sample_count": _DATASET_SAMPLES.get(ds_name, 30),
                        "inference_config": {"temperature": 0.0},
                        "dataset_version": "1.0.0",
                        "dataset_split": "test",
                        "ingested_at": _now(),
                    })
                elif key in failed_runs:
                    # failed: eval_run exists but no eval_result
                    EVAL_RUNS.append({
                        "eval_run_id": run_id,
                        "checkpoint_id": cp_id,
                        "dataset_name": ds_name,
                        "status": "failed",
                        "error_message": failed_runs[key],
                        "grader_type": _DATASET_GRADER.get(ds_name, "exact_match"),
                        "sample_count": _DATASET_SAMPLES.get(ds_name, 30),
                        "inference_config": {"temperature": 0.0},
                        "dataset_version": "1.0.0",
                        "dataset_split": "test",
                        "ingested_at": _now(),
                    })
                else:
                    # completed: both eval_run and eval_result
                    sample_n = _DATASET_SAMPLES.get(ds_name, 30)
                    ci_lower, ci_upper, se, sc = _compute_ci(primary_val, sample_n)
                    EVAL_RUNS.append({
                        "eval_run_id": run_id,
                        "checkpoint_id": cp_id,
                        "dataset_name": ds_name,
                        "status": "completed",
                        "grader_type": _DATASET_GRADER.get(ds_name, "exact_match"),
                        "sample_count": sample_n,
                        "inference_config": {"temperature": 0.0},
                        "dataset_version": "1.0.0",
                        "dataset_split": "test",
                        "ingested_at": _now(),
                    })
                    EVAL_RESULTS.append({
                        "checkpoint_id": cp_id, "dataset_name": ds_name,
                        "metric_name": ds_info["primary"], "metric_value": primary_val,
                        "is_primary": True, "eval_config": {}, "eval_run_id": run_id,
                        "ci_lower": ci_lower, "ci_upper": ci_upper,
                        "stderr": se, "sample_count": sc,
                        "ingested_at": _now(),
                    })
                    for extra_name, offset in ds_info.get("extra", {}).items():
                        EVAL_RESULTS.append({
                            "checkpoint_id": cp_id, "dataset_name": ds_name,
                            "metric_name": extra_name,
                            "metric_value": round(min(1.0, primary_val + offset + random.gauss(0, 0.005)), 4),
                            "is_primary": False, "eval_config": {}, "eval_run_id": run_id,
                            "ingested_at": _now(),
                        })

    # These (model_id, dataset_name) pairs intentionally omit CI to exercise the
    # "insufficient_data" significance path.
    _NO_CI_BASELINE = {
        ("qwen-2.5-72b", "math500"),
        ("claude-3.5-sonnet", "humaneval"),
        ("gpt-4o", "mmlu_pro"),
    }

    for m in BASELINE_MODELS:
        MODELS.append({
            "model_id": m["model_id"], "display_name": m["display_name"],
            "model_type": "baseline", "owner": m["owner"], "created_at": _now(),
            "param_count": None,
            "is_pinned": False,
        })
        cp_id = f"{m['model_id']}__baseline"
        CHECKPOINTS.append({
            "checkpoint_id": cp_id, "model_id": m["model_id"],
            "training_step": None, "checkpoint_path": None,
            "metadata": {}, "created_at": _now(),
        })
        for ds_name, score in BASELINE_SCORES[m["model_id"]].items():
            ds_info = DATASETS[ds_name]
            run_id = f"{cp_id}__{ds_name}"
            no_ci = (m["model_id"], ds_name) in _NO_CI_BASELINE
            sample_n = None if no_ci else _DATASET_SAMPLES.get(ds_name, 30)
            ci_lower, ci_upper, se, sc = _compute_ci(score, sample_n)
            EVAL_RUNS.append({
                "eval_run_id": run_id,
                "checkpoint_id": cp_id,
                "dataset_name": ds_name,
                "status": "completed",
                "grader_type": _DATASET_GRADER.get(ds_name, "exact_match"),
                "sample_count": sample_n,
                "inference_config": {"temperature": 0.0},
                "dataset_version": "1.0.0",
                "dataset_split": "test",
                "ingested_at": _now(),
            })
            EVAL_RESULTS.append({
                "checkpoint_id": cp_id, "dataset_name": ds_name,
                "metric_name": ds_info["primary"], "metric_value": score,
                "is_primary": True, "eval_config": {}, "eval_run_id": run_id,
                "ci_lower": ci_lower, "ci_upper": ci_upper,
                "stderr": se, "sample_count": sc,
                "ingested_at": _now(),
            })

    # Seed benchmark metadata
    _CATEGORIES = {
        "bbh": ("reasoning", "logic"),
        "math500": ("reasoning", "math"),
        "gsm8k": ("reasoning", "math"),
        "humaneval": ("coding", "pass_at_k"),
        "mbpp": ("coding", "pass_at_k"),
        "ifeval": ("instruction_following", None),
        "arc_challenge": ("knowledge", "science"),
        "mmlu_pro": ("knowledge", "multi_domain"),
    }

    for ds_name, (cat, subcat) in _CATEGORIES.items():
        BENCHMARK_METADATA.append({
            "dataset_name": ds_name,
            "category": cat,
            "subcategory": subcat,
            "primary_metric": DATASETS[ds_name]["primary"],
            "description": None,
        })

    # Seed eval suites
    EVAL_SUITES.append({
        "suite_id": "core",
        "display_name": "Core Research Suite",
        "description": "Primary benchmarks for model evaluation",
        "dataset_names": ["bbh", "math500", "humaneval", "gsm8k"],
        "created_at": _now(),
    })
    EVAL_SUITES.append({
        "suite_id": "coding",
        "display_name": "Coding Suite",
        "description": "Code generation benchmarks",
        "dataset_names": ["humaneval", "mbpp"],
        "created_at": _now(),
    })
    EVAL_SUITES.append({
        "suite_id": "full",
        "display_name": "Full Suite",
        "description": "All benchmarks",
        "dataset_names": list(DATASETS.keys()),
        "created_at": _now(),
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
    results = []
    for e in EVAL_RESULTS:
        if e["checkpoint_id"] in cp_ids:
            results.append({
                **e,
                "training_step": cp_step.get(e["checkpoint_id"]),
                "ci_lower": e.get("ci_lower"),
                "ci_upper": e.get("ci_upper"),
                "stderr": e.get("stderr"),
                "sample_count": e.get("sample_count"),
            })
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
    common_only: bool = Query(False, description="Filter to datasets where ALL models have results"),
):
    model_ids = [m.strip() for m in models.split(",") if m.strip()]

    # When common_only is True, restrict to datasets where every requested model has a
    # primary eval result (among the checkpoints for that model).
    if common_only:
        def _model_datasets(mid: str) -> set[str]:
            cp_ids = {c["checkpoint_id"] for c in CHECKPOINTS if c["model_id"] == mid}
            return {e["dataset_name"] for e in EVAL_RESULTS if e["checkpoint_id"] in cp_ids and e["is_primary"]}

        valid_model_ids = [mid for mid in model_ids if any(m["model_id"] == mid for m in MODELS)]
        if valid_model_ids:
            common_datasets: set[str] = _model_datasets(valid_model_ids[0])
            for mid in valid_model_ids[1:]:
                common_datasets &= _model_datasets(mid)
        else:
            common_datasets = set()
        # Override the single-dataset filter: only proceed if dataset is in common set
        if dataset not in common_datasets:
            return {"dataset": dataset, "models": [], "common_only": True, "common_datasets": sorted(common_datasets)}
    else:
        common_datasets = None

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
    resp = {"dataset": dataset, "models": list(result.values())}
    if common_datasets is not None:
        resp["common_datasets"] = sorted(common_datasets)
    return resp


@app.get("/api/heatmap")
async def get_heatmap(suite_id: str | None = Query(None)):
    all_datasets = sorted({e["dataset_name"] for e in EVAL_RESULTS if e["is_primary"]}
                          | {r["dataset_name"] for r in EVAL_RUNS})
    if suite_id is not None:
        suite = next((s for s in EVAL_SUITES if s["suite_id"] == suite_id), None)
        if suite is None:
            return JSONResponse({"error": "Suite not found"}, status_code=404)
        suite_ds = set(suite["dataset_names"])
        datasets = [ds for ds in all_datasets if ds in suite_ds]
    else:
        datasets = all_datasets
    models_out = []
    matrix = {}
    seen = set()
    # Training models first, then baselines
    sorted_models = sorted(MODELS, key=lambda m: (0 if m["model_type"] == "training" else 1, m["model_id"]))
    for m in sorted_models:
        mid = m["model_id"]
        if mid in seen:
            continue
        seen.add(mid)
        models_out.append({
            "model_id": mid, "display_name": m["display_name"],
            "model_type": m["model_type"], "owner": m["owner"],
            "param_count": m.get("param_count"),
            "is_pinned": m.get("is_pinned", False),
        })
        cp_ids = {c["checkpoint_id"] for c in CHECKPOINTS if c["model_id"] == mid}
        # best completed result per dataset: (score, eval_run_id)
        best: dict[str, tuple[float, str]] = {}
        for e in EVAL_RESULTS:
            if e["checkpoint_id"] in cp_ids and e["is_primary"]:
                ds = e["dataset_name"]
                run_id = e.get("eval_run_id", "")
                if ds not in best or e["metric_value"] > best[ds][0]:
                    best[ds] = (e["metric_value"], run_id)
        # collect non-completed runs (pending/failed) that have no completed result
        non_completed: dict[str, dict] = {}
        for r in EVAL_RUNS:
            if r["checkpoint_id"] in cp_ids and r["status"] != "completed":
                ds = r["dataset_name"]
                if ds not in best:
                    # last-write wins for non-completed; prefer failed over pending for display
                    existing = non_completed.get(ds)
                    if existing is None or r["status"] == "failed":
                        non_completed[ds] = r
        cell: dict[str, dict] = {}
        for ds, (score, run_id) in best.items():
            cell[ds] = {"score": round(score, 4), "status": "completed", "eval_run_id": run_id}
        for ds, r in non_completed.items():
            cell[ds] = {"score": None, "status": r["status"], "eval_run_id": r["eval_run_id"]}
        matrix[mid] = cell
    coverage = {}
    for m in models_out:
        mid = m["model_id"]
        evaluated = sum(
            1 for ds in datasets
            if ds in matrix.get(mid, {}) and matrix[mid][ds].get("score") is not None
        )
        missing = [
            ds for ds in datasets
            if ds not in matrix.get(mid, {}) or matrix[mid][ds].get("score") is None
        ]
        coverage[mid] = {"evaluated": evaluated, "total": len(datasets), "missing": missing}
    categories: dict[str, list[str]] = {}
    for bm in BENCHMARK_METADATA:
        cat = bm["category"]
        if cat not in categories:
            categories[cat] = []
        if bm["dataset_name"] in datasets:
            categories[cat].append(bm["dataset_name"])
    return {"models": models_out, "datasets": datasets, "matrix": matrix, "coverage": coverage,
            "categories": categories}


@app.post("/api/admin/suites")
async def create_suite(body: dict):
    existing = next((s for s in EVAL_SUITES if s["suite_id"] == body["suite_id"]), None)
    if existing:
        existing.update(body)
    else:
        body.setdefault("created_at", _now())
        EVAL_SUITES.append(body)
    return {"ok": True, "suite_id": body["suite_id"]}


@app.get("/api/suites")
async def list_suites():
    return {"suites": EVAL_SUITES}


@app.get("/api/suites/{suite_id}/heatmap")
async def get_suite_heatmap(suite_id: str):
    suite = next((s for s in EVAL_SUITES if s["suite_id"] == suite_id), None)
    if not suite:
        return JSONResponse({"error": "Suite not found"}, status_code=404)
    return await get_heatmap(suite_id=suite_id)


@app.post("/api/admin/benchmark-metadata")
async def update_benchmark_metadata(body: dict):
    for bm in body.get("benchmarks", []):
        existing = next((m for m in BENCHMARK_METADATA if m["dataset_name"] == bm["dataset_name"]), None)
        if existing:
            existing.update(bm)
        else:
            BENCHMARK_METADATA.append(bm)
    return {"ok": True}


@app.patch("/api/models/{model_id}")
async def patch_model(model_id: str, body: dict):
    model = next((m for m in MODELS if m["model_id"] == model_id), None)
    if not model:
        return JSONResponse({"error": "Model not found"}, status_code=404)
    if "param_count" in body:
        model["param_count"] = body["param_count"]
    if "is_pinned" in body:
        model["is_pinned"] = body["is_pinned"]
    return {"ok": True, "model_id": model_id}


@app.get("/api/eval-runs/{eval_run_id}")
async def get_eval_run(eval_run_id: str):
    run = next((r for r in EVAL_RUNS if r["eval_run_id"] == eval_run_id), None)
    if not run:
        return JSONResponse({"error": "Eval run not found"}, status_code=404)
    return run


@app.get("/api/models/{model_id}/diagnosis")
async def get_model_diagnosis(model_id: str):
    model = next((m for m in MODELS if m["model_id"] == model_id), None)
    if not model:
        return JSONResponse({"error": "Model not found"}, status_code=404)
    # Latest checkpoint
    cps = sorted(
        [c for c in CHECKPOINTS if c["model_id"] == model_id],
        key=lambda c: c["training_step"] if c["training_step"] is not None else float("inf"),
    )
    if not cps:
        return {"model_id": model_id, "scores": {}, "latest_checkpoint": None}
    latest_cp = cps[-1] if cps[-1]["training_step"] is not None else cps[0]

    # Latest primary scores
    latest_scores = {}
    for e in EVAL_RESULTS:
        if e["checkpoint_id"] == latest_cp["checkpoint_id"] and e["is_primary"]:
            latest_scores[e["dataset_name"]] = e

    # Best baseline per dataset
    baseline_map = {}
    for m in MODELS:
        if m["model_type"] != "baseline":
            continue
        bl_cps = {c["checkpoint_id"] for c in CHECKPOINTS if c["model_id"] == m["model_id"]}
        for e in EVAL_RESULTS:
            if e["checkpoint_id"] in bl_cps and e["is_primary"]:
                ds = e["dataset_name"]
                if ds not in baseline_map or e["metric_value"] > baseline_map[ds]["score"]:
                    baseline_map[ds] = {
                        "score": e["metric_value"],
                        "model": m["display_name"],
                        "ci_lower": e.get("ci_lower"),
                        "ci_upper": e.get("ci_upper"),
                    }

    # Trend: last 5 checkpoints per dataset
    recent_by_ds = {}
    for cp in reversed(cps[-5:]):
        for e in EVAL_RESULTS:
            if e["checkpoint_id"] == cp["checkpoint_id"] and e["is_primary"]:
                ds = e["dataset_name"]
                if ds not in recent_by_ds:
                    recent_by_ds[ds] = []
                recent_by_ds[ds].append(e["metric_value"])

    def compute_significance(m_ci_lo, m_ci_hi, b_ci_lo, b_ci_hi):
        if m_ci_lo is None or b_ci_lo is None:
            return "insufficient_data"
        if m_ci_lo > b_ci_hi or b_ci_lo > m_ci_hi:
            return "likely_real"
        overlap = min(m_ci_hi, b_ci_hi) - max(m_ci_lo, b_ci_lo)
        smaller = min(m_ci_hi - m_ci_lo, b_ci_hi - b_ci_lo)
        if smaller > 0 and overlap / smaller > 0.5:
            return "likely_noise"
        return "uncertain"

    def compute_trend(values):
        if len(values) < 2:
            return "new"
        delta = values[-1] - values[-2]
        if delta > 0.02:
            return "up"
        if delta < -0.02:
            return "down"
        if len(values) >= 3:
            long_delta = values[-1] - values[0]
            if long_delta > 0.03:
                return "up_slow"
            if long_delta < -0.03:
                return "down_slow"
        return "flat"

    scores = {}
    for ds, e in latest_scores.items():
        bl = baseline_map.get(ds, {})
        val = e["metric_value"]
        bl_score = bl.get("score")
        m_ci_lo = e.get("ci_lower")
        m_ci_hi = e.get("ci_upper")
        b_ci_lo = bl.get("ci_lower")
        b_ci_hi = bl.get("ci_upper")
        scores[ds] = {
            "value": round(val, 4),
            "metric_name": e["metric_name"],
            "baseline_best": round(bl_score, 4) if bl_score is not None else None,
            "baseline_model": bl.get("model"),
            "gap": round(val - bl_score, 4) if bl_score is not None else None,
            "trend": compute_trend(recent_by_ds.get(ds, [])),
            "ci_lower": m_ci_lo,
            "ci_upper": m_ci_hi,
            "stderr": e.get("stderr"),
            "sample_count": e.get("sample_count"),
            "significance": compute_significance(m_ci_lo, m_ci_hi, b_ci_lo, b_ci_hi),
        }

    # best_overall_step: checkpoint with highest average primary metric across all datasets
    cp_avgs: dict[str, float] = {}
    for cp in cps:
        if cp["training_step"] is None:
            continue
        vals = [e["metric_value"] for e in EVAL_RESULTS
                if e["checkpoint_id"] == cp["checkpoint_id"] and e["is_primary"]]
        if vals:
            cp_avgs[cp["checkpoint_id"]] = sum(vals) / len(vals)
    best_overall_cp_id = max(cp_avgs, key=cp_avgs.__getitem__) if cp_avgs else None
    best_overall_step = next(
        (c["training_step"] for c in cps if c["checkpoint_id"] == best_overall_cp_id), None
    )

    # best_per_dataset: dataset_name → best training_step
    best_per_dataset: dict[str, int | None] = {}
    for cp in cps:
        if cp["training_step"] is None:
            continue
        for e in EVAL_RESULTS:
            if e["checkpoint_id"] == cp["checkpoint_id"] and e["is_primary"]:
                ds = e["dataset_name"]
                cur_best_step = best_per_dataset.get(ds)
                if cur_best_step is None:
                    best_per_dataset[ds] = cp["training_step"]
                else:
                    # compare score for this ds at cur_best vs this step
                    cur_best_score = next(
                        (ev["metric_value"] for ev in EVAL_RESULTS
                         if ev["checkpoint_id"] == f"{model_id}__step-{cur_best_step}"
                         and ev["dataset_name"] == ds and ev["is_primary"]),
                        None,
                    )
                    if cur_best_score is None or e["metric_value"] > cur_best_score:
                        best_per_dataset[ds] = cp["training_step"]

    return {
        "model_id": model_id,
        "display_name": model["display_name"],
        "model_type": model["model_type"],
        "latest_checkpoint": latest_cp["checkpoint_id"],
        "latest_step": latest_cp["training_step"],
        "best_overall_step": best_overall_step,
        "best_per_dataset": best_per_dataset,
        "scores": scores,
    }


@app.get("/api/filters")
async def get_filter_options():
    return {
        "owners": sorted({m["owner"] for m in MODELS}),
        "model_types": sorted({m["model_type"] for m in MODELS}),
        "datasets": sorted({e["dataset_name"] for e in EVAL_RESULTS}),
    }


@app.delete("/api/models/{model_id}")
async def delete_model(model_id: str):
    global MODELS, CHECKPOINTS, EVAL_RESULTS, EVAL_RUNS
    if not any(m["model_id"] == model_id for m in MODELS):
        return JSONResponse({"error": "Model not found"}, status_code=404)
    cp_ids = {c["checkpoint_id"] for c in CHECKPOINTS if c["model_id"] == model_id}
    EVAL_RESULTS = [e for e in EVAL_RESULTS if e["checkpoint_id"] not in cp_ids]
    EVAL_RUNS = [r for r in EVAL_RUNS if r["checkpoint_id"] not in cp_ids]
    CHECKPOINTS = [c for c in CHECKPOINTS if c["model_id"] != model_id]
    MODELS = [m for m in MODELS if m["model_id"] != model_id]
    return {"ok": True, "deleted": model_id}


@app.delete("/api/checkpoints/{checkpoint_id}")
async def delete_checkpoint(checkpoint_id: str):
    global CHECKPOINTS, EVAL_RESULTS, EVAL_RUNS
    if not any(c["checkpoint_id"] == checkpoint_id for c in CHECKPOINTS):
        return JSONResponse({"error": "Checkpoint not found"}, status_code=404)
    EVAL_RESULTS = [e for e in EVAL_RESULTS if e["checkpoint_id"] != checkpoint_id]
    EVAL_RUNS = [r for r in EVAL_RUNS if r["checkpoint_id"] != checkpoint_id]
    CHECKPOINTS = [c for c in CHECKPOINTS if c["checkpoint_id"] != checkpoint_id]
    return {"ok": True, "deleted": checkpoint_id}


def main():
    parser = argparse.ArgumentParser(description="Eval360 Dashboard Viewer (Mock)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=11001)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    seed_data()
    logger.info("Seeded %d models, %d checkpoints, %d eval results, %d eval runs",
                len(MODELS), len(CHECKPOINTS), len(EVAL_RESULTS), len(EVAL_RUNS))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
