r"""Harvest all live cyclingoracle TdF-2026 stage predictions to local memory.

Part 2 (data preservation + prediction-model replication): iterate the
cyclingoracle blog index for the Tour de France 2026 men's race, pull every
per-stage win-probability prediction (enriched with full rider names so they
match Scorito), and persist one JSONL row per rider per stage.

Usage:
    $env:PYTHONPATH="src"; $env:PYTHONUTF8="1"; $env:NO_PROXY="*"; $env:HTTP_TRANSPORT="auto"
    .\.venv\Scripts\python.exe scripts\harvest_cyclingoracle.py

Output:
    data/cyclingoracle/tdf2026_predictions.jsonl
"""

from __future__ import annotations

import sys
from pathlib import Path

from scorito_agent.cyclingoracle.scraper import (
    list_stages,
    stage_predictions,
    write_jsonl,
)

RACE_SLUG = "tour-de-france-2026"
OUT_PATH = Path("data/cyclingoracle/tdf2026_predictions.jsonl")


def main() -> int:
    posts = list_stages(RACE_SLUG)
    # Men's race only: drop the women's "Tour de France Femmes" posts, keep
    # only per-stage prediction posts (not race-overall or data-list posts).
    stage_posts = [
        p
        for p in posts
        if p.get("kind") == "stage_prediction"
        and "femmes" not in (p.get("slug") or "").lower()
    ]

    print(f"Found {len(posts)} blog posts for {RACE_SLUG!r}; "
          f"{len(stage_posts)} are men's stage predictions.")

    rows: list[dict] = []
    seen_stage_numbers: list[int] = []
    for post in stage_posts:
        url = post["url"]
        stage_number = post.get("stage_number")
        try:
            preds = stage_predictions(url, enrich=True, cache=True)
        except Exception as exc:  # noqa: BLE001 — keep harvesting other stages
            print(f"  [skip] stage {stage_number} {url}: {exc}")
            continue
        if not preds:
            print(f"  [empty] stage {stage_number} {url}: no prediction rows")
            continue
        # Add a `model_rank` alias so the external.py adapter can read it
        # directly (it falls back to enumerate order, but be explicit).
        for row in preds:
            row.setdefault("model_rank", row.get("predicted_rank"))
        rows.extend(preds)
        seen_stage_numbers.append(stage_number)
        top = preds[0]
        print(
            f"  stage {stage_number:>2}: {len(preds):>2} riders — "
            f"top: {top.get('rider_name')} "
            f"({top.get('win_probability_pct')}%)"
        )

    write_jsonl(OUT_PATH, rows)
    print(
        f"\nWrote {len(rows)} rows across {len(seen_stage_numbers)} stages "
        f"to {OUT_PATH}"
    )
    print(f"Stages harvested: {sorted(n for n in seen_stage_numbers if n is not None)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
