"""Seed the PCS StageStore from finished Scorito stage snapshots (Part 3).

For every finished Scorito stage we have local results for (tdf2026, giro2026),
build a PCS-style stage record and upsert it into ``data/pcs/stages.json``.

The record maps Scorito enums onto the predictor's tokens so the offline
similarity model (``scorito_agent.pcs.predict``) can be leave-one-out validated
without any live ProCyclingStats access:

  profile_type  <- terrain_type (1 Flat->"flat", 2 Hilly->"hilly",
                   3 Mountain->"mountain"), overridden to "itt"/"ttt" when the
                   stage is an ITT/TTT (stage_type 2/3).
  finish_type   <- heuristic (ITT/TTT->"tt", Mountain->"summit",
                   Hilly->"uphill", Flat->"sprint").
  startlist     <- every snapshot rider as {rider, rider_slug, team}.
  results       <- riders ranked by realized per-stage Scorito points
                   (rank 1 == most points), each carrying its realized ``points``.

Usage:
  python scripts/build_pcs_store.py [slug ...]      # default: tdf2026 giro2026
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scorito_agent.pcs.slugs import slug_from_name  # noqa: E402
from scorito_agent.pcs.store import StageStore  # noqa: E402
from scorito_agent.scorito.loader import load_snapshot  # noqa: E402

DEFAULT_SLUGS = ["tdf2026", "giro2026"]
YEAR = 2026

_TERRAIN_PROFILE = {1: "flat", 2: "hilly", 3: "mountain"}
_TERRAIN_FINISH = {1: "sprint", 2: "uphill", 3: "summit"}


def profile_type_for(stage) -> str:
    if stage.stage_type == 2:
        return "itt"
    if stage.stage_type == 3:
        return "ttt"
    return _TERRAIN_PROFILE.get(stage.terrain_type, "unknown")


def finish_type_for(stage) -> str:
    if stage.stage_type in (2, 3):
        return "tt"
    return _TERRAIN_FINISH.get(stage.terrain_type, "unknown")


def build_stage_record(snapshot, stage, race_slug: str, year: int) -> dict:
    startlist = [
        {
            "rider": rider.name,
            "rider_slug": slug_from_name(rider.name),
            "team": str(rider.team_id),
        }
        for rider in snapshot.riders
    ]

    scored = []
    for rider in snapshot.riders:
        points = float(snapshot.actual_points(rider.rider_id, stage))
        if points > 0.0:
            scored.append((rider, points))
    scored.sort(key=lambda item: item[1], reverse=True)

    results = []
    for rank, (rider, points) in enumerate(scored, start=1):
        results.append(
            {
                "rider": rider.name,
                "rider_slug": slug_from_name(rider.name),
                "rank": rank,
                "team": str(rider.team_id),
                "points": points,
            }
        )

    stage_no = stage.order
    return {
        "id": f"{race_slug}::{year}::{stage_no}",
        "race": race_slug,
        "year": year,
        "stage_no": stage_no,
        "stage_type": stage.stage_type,
        "terrain_type": stage.terrain_type,
        "stage_type_label": stage.stage_type_label,
        "terrain_label": stage.terrain_label,
        "profile_type": profile_type_for(stage),
        "finish_type": finish_type_for(stage),
        "startlist": startlist,
        "results": results,
    }


def main() -> int:
    slugs = sys.argv[1:] or DEFAULT_SLUGS
    store = StageStore()
    store.clear()

    total = 0
    for slug in slugs:
        snapshot = load_snapshot(slug)
        stored = 0
        with_results = 0
        for stage in snapshot.stages:
            record = build_stage_record(snapshot, stage, slug, YEAR)
            store.upsert_stage(record)
            stored += 1
            total += 1
            if record["results"]:
                with_results += 1
        print(
            f"{slug}: {len(snapshot.riders)} riders, "
            f"{stored} stages stored ({with_results} with results)"
        )

    # Summary of the profile-type distribution now in the store.
    stages = store.all_stages()
    dist: dict[str, int] = {}
    for stage in stages:
        key = f"{stage.get('profile_type')}/{stage.get('finish_type')}"
        dist[key] = dist.get(key, 0) + 1
    print(f"\nStored {total} stages -> {store.path}")
    for key in sorted(dist):
        print(f"  {key:20s} {dist[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
