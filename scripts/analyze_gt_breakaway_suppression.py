"""Quantify early summit-stage breakaway conversion and persist model priors."""

from __future__ import annotations

import csv
import json
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scorito_agent.breakaway import (  # noqa: E402
    EARLY_STAGE_MAX,
    UNIPUERTO_VERTICAL_METERS_MAX,
    historical_breakaway_prior,
)

LABELS_PATH = ROOT / "data" / "pcs" / "gt_summit_breakaway_labels.csv"
OUTPUT_PATH = ROOT / "data" / "pcs" / "gt_summit_breakaway_analysis.json"


def load_records(path: Path = LABELS_PATH) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["year"] = int(row["year"])
        row["stage_no"] = int(row["stage_no"])
        row["breakaway_win"] = int(row["breakaway_win"])
        row["vertical_meters"] = int(row["vertical_meters"])
        row["profile_score"] = int(row["profile_score"])
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


def summarize(
    records: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    selected = [record for record in records if predicate(record)]
    wins = sum(record["breakaway_win"] for record in selected)
    total = len(selected)
    return {
        "wins": wins,
        "stages": total,
        "rate": wins / total if total else None,
        "wilson_95": wilson_interval(wins, total),
    }


def corrected_odds_ratio(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_losses = first["stages"] - first["wins"]
    second_losses = second["stages"] - second["wins"]
    return ((first["wins"] + 0.5) * (second_losses + 0.5)) / (
        (first_losses + 0.5) * (second["wins"] + 0.5)
    )


def fisher_exact_two_sided(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_total = int(first["stages"])
    wins_total = int(first["wins"] + second["wins"])
    total = int(first["stages"] + second["stages"])
    observed = int(first["wins"])

    def probability(wins_in_first: int) -> float:
        return (
            math.comb(wins_total, wins_in_first)
            * math.comb(total - wins_total, first_total - wins_in_first)
            / math.comb(total, first_total)
        )

    minimum = max(0, first_total - (total - wins_total))
    maximum = min(first_total, wins_total)
    observed_probability = probability(observed)
    return sum(
        probability(candidate)
        for candidate in range(minimum, maximum + 1)
        if probability(candidate) <= observed_probability + 1e-12
    )


def cross_validated_brier(records: list[dict[str, Any]]) -> dict[str, float]:
    baseline_errors: list[float] = []
    candidate_errors: list[float] = []
    groups = sorted({(record["race"], record["year"]) for record in records})
    for group in groups:
        training = [
            record
            for record in records
            if (record["race"], record["year"]) != group
        ]
        test = [
            record
            for record in records
            if (record["race"], record["year"]) == group
        ]
        for record in test:
            stage = {
                "stage_no": record["stage_no"],
                "profile_type": "mountain",
                "finish_type": "summit",
                "vertical_meters": record["vertical_meters"],
            }
            prior = historical_breakaway_prior(training, stage)
            outcome = record["breakaway_win"]
            baseline_errors.append((prior["global_rate"] - outcome) ** 2)
            candidate_errors.append((prior["probability"] - outcome) ** 2)
    baseline = sum(baseline_errors) / len(baseline_errors)
    candidate = sum(candidate_errors) / len(candidate_errors)
    return {
        "folds": float(len(groups)),
        "stages": float(len(records)),
        "baseline_brier": baseline,
        "candidate_brier": candidate,
        "absolute_improvement": baseline - candidate,
        "relative_improvement": (baseline - candidate) / baseline,
    }


def build_analysis(records: list[dict[str, Any]]) -> dict[str, Any]:
    early = summarize(records, lambda record: record["stage_no"] <= EARLY_STAGE_MAX)
    later = summarize(records, lambda record: record["stage_no"] > EARLY_STAGE_MAX)
    unipuerto = summarize(
        records,
        lambda record: record["vertical_meters"] <= UNIPUERTO_VERTICAL_METERS_MAX,
    )
    other_summits = summarize(
        records,
        lambda record: record["vertical_meters"] > UNIPUERTO_VERTICAL_METERS_MAX,
    )
    target_stage = {
        "stage_no": 3,
        "profile_type": "mountain",
        "finish_type": "summit",
        "vertical_meters": 2639,
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "input": str(LABELS_PATH.relative_to(ROOT)).replace("\\", "/"),
        "sample_definition": (
            "Men's Giro, Tour and Vuelta mountain stages with summit finish, "
            "2024-2026, present in the repository PCS context cache and manually "
            "labelled as established-break win or GC-group win."
        ),
        "definitions": {
            "early": f"stage number <= {EARLY_STAGE_MAX}",
            "unipuerto_like": (
                "mountain summit stage with total vertical meters <= "
                f"{UNIPUERTO_VERTICAL_METERS_MAX}; route-shape proxy, not climb count"
            ),
            "breakaway_success": "winner came from an established breakaway",
            "formation": "not observed in the PCS cache",
            "kom_incentive": "not observed in the PCS cache",
        },
        "overall": summarize(records, lambda _: True),
        "early": early,
        "later": later,
        "early_effect": {
            "risk_difference": early["rate"] - later["rate"],
            "haldane_corrected_odds_ratio": corrected_odds_ratio(early, later),
            "fisher_exact_two_sided_p": fisher_exact_two_sided(early, later),
        },
        "unipuerto_like": unipuerto,
        "other_summits": other_summits,
        "unipuerto_effect": {
            "risk_difference": unipuerto["rate"] - other_summits["rate"],
            "haldane_corrected_odds_ratio": corrected_odds_ratio(
                unipuerto, other_summits
            ),
            "fisher_exact_two_sided_p": fisher_exact_two_sided(
                unipuerto, other_summits
            ),
        },
        "validation": cross_validated_brier(records),
        "vuelta_2026_stage_3_prior": historical_breakaway_prior(
            records, target_stage
        ),
        "limitations": [
            "Only 28 cached summit stages and five early stages; intervals are wide.",
            "Break formation, break composition and intermediate KOM contests are absent.",
            "Post-stage GC rank is retained for audit but is not a pre-stage GC-gap control.",
            "Vertical meters is a unipuerto proxy and cannot identify the number or placement of climbs.",
            "Year, race, field, chase incentives and route severity remain partly confounded.",
            "Associations are predictive priors, not causal estimates of team permission.",
        ],
    }


def main() -> int:
    analysis = build_analysis(load_records())
    OUTPUT_PATH.write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(analysis, ensure_ascii=False, indent=2))
    print(f"Saved: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
