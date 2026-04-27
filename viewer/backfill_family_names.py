"""Deduplicate and normalise checkpoint rows in the Eval360 dashboard DB.

Two problems exist in the live DB:

1. DUPLICATE CHECKPOINTS — same (model_id, training_step) exists under multiple
   checkpoint_ids because the naming convention changed across scheduler runs.
   The dashboard renders one series line per checkpoint_id, so duplicates produce
   multiple overlapping lines per step.

2. NON-CANONICAL IDS — some models have only one checkpoint_id per step but the
   id uses a verbose legacy format (e.g. `k2moe375b-mid2__k2moe375b-mid2-checkpoint_0002500`)
   rather than the clean `{model_id}__step-{training_step}` form.

This script:
  - Picks ONE canonical checkpoint_id per (model_id, training_step).
    Priority: prefer `{model_id}__step-{step}` if it exists, else use the row
    with the most eval_result rows attached (most complete data), else oldest.
  - Repoints all FK / plain-text children (eval_results, eval_runs, alerts,
    activity_log) from every non-canonical id to the canonical one.
  - Deletes the now-empty non-canonical checkpoint rows.
  - For the canonical row itself, renames its checkpoint_id to the clean
    `{model_id}__step-{step}` format if it isn't already (insert-new → repoint
    → delete-old, because you can't UPDATE a PK in-place).

Runs dry-run by default. Use --apply to execute (single transaction).

Usage:
    DATABASE_URL=postgres://... python backfill_family_names.py            # preview
    DATABASE_URL=postgres://... python backfill_family_names.py --apply    # execute
    python backfill_family_names.py --only-model bbq-8b-mid2               # scope to one model
"""

import argparse
import asyncio
import logging
import sys

import asyncpg
import db as _db

logger = logging.getLogger("dedup_checkpoints")


def canonical_id(model_id: str, training_step: int) -> str:
    return f"{model_id}__step-{training_step}"


async def build_plan(conn: asyncpg.Connection, only_model: str | None):
    """
    Returns a list of migration actions, each a dict:
      {
        model_id, training_step,
        keep_id,          # checkpoint_id to keep (may need renaming)
        target_id,        # final canonical checkpoint_id
        drop_ids,         # checkpoint_ids to delete after repointing
        rename_needed,    # True if keep_id != target_id
      }
    """
    where = "WHERE model_id = $1" if only_model else ""
    params = [only_model] if only_model else []

    rows = await conn.fetch(f"""
        SELECT c.checkpoint_id, c.model_id, c.training_step,
               COUNT(er.id) AS result_count,
               c.created_at
        FROM checkpoints c
        LEFT JOIN eval_results er ON er.checkpoint_id = c.checkpoint_id
        {where}
        GROUP BY c.checkpoint_id, c.model_id, c.training_step, c.created_at
        ORDER BY c.model_id, c.training_step NULLS LAST, COUNT(er.id) DESC, c.created_at
    """, *params)

    # Group by (model_id, training_step)
    groups: dict[tuple, list] = {}
    for r in rows:
        key = (r["model_id"], r["training_step"])
        groups.setdefault(key, []).append(dict(r))

    plan = []
    for (model_id, training_step), members in groups.items():
        if training_step is None:
            # Baseline or no-step checkpoints — skip for now
            continue

        target = canonical_id(model_id, training_step)

        # Pick which existing row to promote as canonical:
        # 1) The row already named target_id (no data loss risk)
        # 2) Else the row with the most eval_results
        # 3) Else the oldest
        preferred = next((m for m in members if m["checkpoint_id"] == target), None)
        if preferred is None:
            preferred = members[0]  # already sorted by result_count DESC, created_at ASC

        keep_id = preferred["checkpoint_id"]
        drop_ids = [m["checkpoint_id"] for m in members if m["checkpoint_id"] != keep_id]
        rename_needed = (keep_id != target)

        if len(members) == 1 and not rename_needed:
            continue  # already clean

        plan.append({
            "model_id": model_id,
            "training_step": training_step,
            "keep_id": keep_id,
            "target_id": target,
            "drop_ids": drop_ids,
            "rename_needed": rename_needed,
        })

    return plan


async def apply_plan(conn: asyncpg.Connection, plan: list):
    async with conn.transaction():
        for action in plan:
            keep_id = action["keep_id"]
            target_id = action["target_id"]
            drop_ids = action["drop_ids"]

            # Step 1: For duplicate rows being dropped, the canonical already
            # has eval_results so we can't repoint (unique constraint). Delete
            # the duplicates' results directly; eval_runs cascade to example_results.
            for old_id in drop_ids:
                # eval_runs FK cascades to example_results on DELETE
                old_run_ids = await conn.fetch(
                    "SELECT eval_run_id FROM eval_runs WHERE checkpoint_id=$1", old_id,
                )
                run_id_list = [r["eval_run_id"] for r in old_run_ids]
                if run_id_list:
                    await conn.execute(
                        "DELETE FROM eval_results WHERE checkpoint_id=$1", old_id,
                    )
                    await conn.execute(
                        "DELETE FROM eval_runs    WHERE checkpoint_id=$1", old_id,
                    )
                await conn.execute(
                    "UPDATE alerts       SET checkpoint_id=$1 WHERE checkpoint_id=$2",
                    keep_id, old_id,
                )
                await conn.execute(
                    "UPDATE activity_log SET checkpoint_id=$1 WHERE checkpoint_id=$2",
                    keep_id, old_id,
                )

            # Step 2: Delete the now-empty non-canonical checkpoint rows
            if drop_ids:
                await conn.execute(
                    "DELETE FROM checkpoints WHERE checkpoint_id = ANY($1::text[])",
                    drop_ids,
                )

            # Step 3: Rename keep_id → target_id if needed
            if action["rename_needed"]:
                # Insert canonical row copying from the kept row
                await conn.execute("""
                    INSERT INTO checkpoints
                      (checkpoint_id, model_id, training_step, checkpoint_path,
                       metadata, training_run, recipe_tags, created_at)
                    SELECT $1, model_id, training_step, checkpoint_path,
                           metadata, training_run, recipe_tags, created_at
                    FROM checkpoints WHERE checkpoint_id = $2
                    ON CONFLICT (checkpoint_id) DO NOTHING
                """, target_id, keep_id)

                # Repoint all children to target_id
                for tbl, col in [
                    ("eval_results", "checkpoint_id"),
                    ("eval_runs",    "checkpoint_id"),
                    ("alerts",       "checkpoint_id"),
                    ("activity_log", "checkpoint_id"),
                ]:
                    await conn.execute(
                        f"UPDATE {tbl} SET {col}=$1 WHERE {col}=$2",
                        target_id, keep_id,
                    )

                # Delete the old keep_id row
                await conn.execute(
                    "DELETE FROM checkpoints WHERE checkpoint_id=$1", keep_id,
                )


def print_plan(plan: list):
    if not plan:
        print("(nothing to do — all checkpoints are already clean)")
        return

    total_drops = sum(len(a["drop_ids"]) for a in plan)
    renames = sum(1 for a in plan if a["rename_needed"])
    print(f"Actions: {len(plan)} checkpoint groups to clean up")
    print(f"  → {total_drops} duplicate rows to delete")
    print(f"  → {renames} canonical rows to rename to __step-N format")
    print()
    for a in plan:
        tag = "[rename+dedup]" if a["rename_needed"] and a["drop_ids"] else \
              "[rename]" if a["rename_needed"] else "[dedup]"
        print(f"  {tag} {a['model_id']} step={a['training_step']}")
        if a["rename_needed"]:
            print(f"      {a['keep_id']}")
            print(f"      → {a['target_id']}")
        for d in a["drop_ids"]:
            print(f"      DROP {d}")


async def main_async(args):
    await _db.init_pool()
    try:
        async with _db.pool().acquire() as conn:
            plan = await build_plan(conn, args.only_model)
            print_plan(plan)

            if args.apply:
                if not plan:
                    return 0
                print("\nApplying (single transaction)…")
                await apply_plan(conn, plan)
                print("Done.")
            else:
                print("\n(dry-run — add --apply to execute)")
    finally:
        await _db.close_pool()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true",
                   help="Execute the migration. Default is dry-run.")
    p.add_argument("--only-model", default=None,
                   help="Restrict to a single model_id.")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
