"""Quantify hilly-stage bunch retention and persist the GC-mingling gate evidence."""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scorito_agent.gc_bunch_mingling import (  # noqa: E402
    GRADIENT_CEILING_PCT,
    VERTICAL_METERS_MAX,
    VERTICAL_METERS_MIN,
)

LABELS_PATH = ROOT / "data" / "historical" / "gt_hilly_bunch_mingling_labels.csv"
OUTPUT_PATH = ROOT / "data" / "pcs" / "gt_hilly_bunch_mingling_analysis.json"
FRONT_GROUP_SIZE_THRESHOLD = 10


def load_records(path: Path = LABELS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["year"] = int(row["year"])
        row["stage_no"] = int(row["stage_no"])
        row["vertical_meters"] = int(row["vertical_meters"])
        row["gradient_final_km"] = float(row["gradient_final_km"])
        row["front_group_size"] = int(row["front_group_size"])
    return rows


def wilson_interval(wins: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total == 0:
        return [0.0, 1.0]
    proportion = wins / total
    denominator = 1.0 + z * z / total
    center = (proportion + z * z / (2.0 * total)) / denominator
    margin = (
        z
        * math.sqrt(proportion * (1.0 - proportion) / total + z * z / (4.0 * total * total))
        / denominator
    )
    return [center - margin, center + margin]


def bunch_rate(records: list[dict[str, Any]], threshold: int = FRONT_GROUP_SIZE_THRESHOLD) -> dict[str, Any]:
    wins = sum(1 for row in records if row["front_group_size"] >= threshold)
    total = len(records)
    lo, hi = wilson_interval(wins, total)
    return {
        "wins": wins,
        "total": total,
        "rate": round(wins / total, 4) if total else None,
        "wilson_95ci": [round(lo, 3), round(hi, 3)],
    }


def build_analysis(records: list[dict[str, Any]]) -> dict[str, Any]:
    hilly = [row for row in records if row["profile_type"] == "hilly"]
    below_all = [row for row in hilly if row["gradient_final_km"] < GRADIENT_CEILING_PCT]
    above_all = [row for row in hilly if row["gradient_final_km"] >= GRADIENT_CEILING_PCT]
    below_uphill = [row for row in below_all if row["finish_type"] == "uphill"]
    above_uphill = [row for row in above_all if row["finish_type"] == "uphill"]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "source_labels": str(LABELS_PATH.relative_to(ROOT)).replace("\\", "/"),
        "question": (
            "Does a hilly-profile GT stage keep a front group of "
            f">={FRONT_GROUP_SIZE_THRESHOLD} finishers (evidence a reduced "
            "bunch, not just a small elite move, contested the line)?"
        ),
        "gradient_ceiling_pct": GRADIENT_CEILING_PCT,
        "vertical_band_meters": [VERTICAL_METERS_MIN, VERTICAL_METERS_MAX],
        "front_group_size_threshold": FRONT_GROUP_SIZE_THRESHOLD,
        "all_hilly_stages_below_ceiling": bunch_rate(below_all),
        "all_hilly_stages_at_or_above_ceiling": bunch_rate(above_all),
        "hilly_uphill_finish_below_ceiling": bunch_rate(below_uphill),
        "hilly_uphill_finish_at_or_above_ceiling": bunch_rate(above_uphill),
        "note": (
            "Both slices agree directionally: stages at or above the gradient "
            "ceiling never kept a front group of "
            f"{FRONT_GROUP_SIZE_THRESHOLD}+ riders in this sample, while stages "
            "below it did in roughly a third of cases. Treat this as a "
            "probabilistic gate: it unlocks GC credibility as an alternate "
            "sprint-survival path, it never guarantees a reduced bunch and "
            "never adds a standalone score bonus."
        ),
    }


def main() -> None:
    records = load_records()
    analysis = build_analysis(records)
    OUTPUT_PATH.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(analysis, indent=2))


if __name__ == "__main__":
    main()