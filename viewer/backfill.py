"""Backfill CLI: import existing Eval360 _scores.yaml files into the dashboard.

Usage:
    python backfill.py \
      --output-dir /path/to/Eval360-V2/output/k2-think-v2/ \
      --model-id k2-think-v2 \
      --model-type training \
      --dashboard-url http://localhost:11001 \
      --token <ingest-token>
"""

import argparse
import re
import sys
from pathlib import Path

import httpx


def parse_scores_yaml(path: Path) -> dict[str, float]:
    """Parse an Eval360 _scores.yaml file.

    Format is lines like: "metric_name": 0.1234
    """
    scores = {}
    for line in path.read_text().strip().splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r'^"([^"]+)":\s*([0-9.eE+-]+)$', line)
        if m:
            scores[m.group(1)] = float(m.group(2))
    return scores


def extract_training_step(name: str) -> int | None:
    """Try to extract a training step from a checkpoint directory name.

    Looks for trailing integers: checkpoint-5 -> 5, step-5000 -> 5000.
    """
    m = re.search(r'[-_](\d+)$', name)
    return int(m.group(1)) if m else None


def main():
    parser = argparse.ArgumentParser(description="Backfill eval scores into the Eval360 dashboard")
    parser.add_argument("--output-dir", required=True, help="Path to model output directory containing *_scores.yaml")
    parser.add_argument("--model-id", required=True, help="Model slug (e.g., k2-think-v2)")
    parser.add_argument("--display-name", default=None, help="Human-friendly model name (default: model-id)")
    parser.add_argument("--model-type", required=True, choices=["training", "baseline"])
    parser.add_argument("--owner", default="backfill", help="Owner name")
    parser.add_argument("--training-step", type=int, default=None, help="Explicit training step")
    parser.add_argument("--checkpoint-id", default=None, help="Explicit checkpoint_id")
    parser.add_argument("--primary-metric", default=None, help="Primary metric name (default: first key)")
    parser.add_argument("--dashboard-url", required=True, help="Dashboard base URL")
    parser.add_argument("--token", required=True, help="Ingest bearer token")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        print(f"Error: {output_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    score_files = sorted(output_dir.glob("*_scores.yaml"))
    if not score_files:
        print(f"No *_scores.yaml files found in {output_dir}", file=sys.stderr)
        sys.exit(1)

    dir_name = output_dir.name
    checkpoint_id = args.checkpoint_id or f"{args.model_id}__{dir_name}"
    training_step = args.training_step or extract_training_step(dir_name)
    display_name = args.display_name or args.model_id

    if args.model_type == "baseline":
        checkpoint_id = args.checkpoint_id or f"{args.model_id}__baseline"
        training_step = None

    client = httpx.Client(timeout=30)
    ok_count = 0
    fail_count = 0

    for sf in score_files:
        ds_match = re.match(r'^(.+)_scores\.yaml$', sf.name)
        if not ds_match:
            print(f"  Skipping {sf.name} (doesn't match *_scores.yaml pattern)")
            continue
        dataset_name = ds_match.group(1)
        scores = parse_scores_yaml(sf)
        if not scores:
            print(f"  Skipping {sf.name} (no scores parsed)")
            continue

        primary = args.primary_metric or next(iter(scores))
        payload = {
            "model_id": args.model_id, "display_name": display_name,
            "model_type": args.model_type, "owner": args.owner,
            "checkpoint_id": checkpoint_id, "training_step": training_step,
            "checkpoint_path": str(output_dir), "dataset_name": dataset_name,
            "metrics": scores, "primary_metric": primary,
            "eval_config": {}, "metadata": {},
        }

        try:
            resp = client.post(
                f"{args.dashboard_url}/api/ingest/eval-result",
                json=payload,
                headers={"Authorization": f"Bearer {args.token}"},
            )
            if resp.status_code == 200:
                ok_count += 1
                print(f"  OK: {dataset_name} ({len(scores)} metrics)")
            else:
                fail_count += 1
                print(f"  FAIL: {dataset_name} -> {resp.status_code}: {resp.text}")
        except httpx.HTTPError as e:
            fail_count += 1
            print(f"  FAIL: {dataset_name} -> {e}")

    total = ok_count + fail_count
    print(f"\nIngested {ok_count}/{total} score files for {args.model_id}")
    if fail_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
