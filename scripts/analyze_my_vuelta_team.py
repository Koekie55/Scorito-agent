"""Analyze the signed-in user's Vuelta squad against projected stage top 20s."""
from __future__ import annotations

import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from scorito_agent.scorito import load_snapshot  # noqa: E402
from scripts.refresh_vuelta_stage_predictions import SCORITO_STAGE_POINTS  # noqa: E402

DATA_DIR = ROOT / "data" / "scorito" / "vuelta2026"
PREDICTIONS_PATH = DATA_DIR / "stage_top20_predictions.json"
PROJECTION_PATH = DATA_DIR / "projected_recommendation.json"
PERSONAL_PATH = DATA_DIR / "personal" / "teamselection.json"
OUTPUT_PATH = DATA_DIR / "personal_team_full_analysis.json"
STAGES_CSV_PATH = DATA_DIR / "personal_team_stage_scores.csv"
RIDERS_CSV_PATH = DATA_DIR / "personal_team_rider_scores.csv"
SQUAD_SIZE = 20
LINEUP_SIZE = 9
MAX_RIDERS_PER_TEAM = 4
STAGE_WIN_TEAM_BONUS = 10.0
RED_JERSEY_TEAM_BONUS = 8.0
PROJECTED_RED_JERSEY_RIDER = "Tadej Pogacar"


def name_key(name: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(sorted(re.findall(r"[a-z0-9]+", ascii_name.lower())))


def _rank_points(rank: int | None) -> float:
    return float(SCORITO_STAGE_POINTS.get(rank or 0, 0.0))


def _lineup_sort_key(
    row: tuple[Any, ...],
) -> tuple[float, float, int, str]:
    return (-float(row[1]), -float(row[6]), int(row[2]), str(row[3]))


def _personal_ids() -> list[int]:
    payload = json.loads(PERSONAL_PATH.read_text(encoding="utf-8"))
    rider_ids = payload.get("Content", []) if isinstance(payload, dict) else payload
    if not isinstance(rider_ids, list) or not all(isinstance(rider_id, int) for rider_id in rider_ids):
        raise RuntimeError("personal teamselection is not a rider-id list")
    if len(rider_ids) != SQUAD_SIZE or len(set(rider_ids)) != SQUAD_SIZE:
        raise RuntimeError("personal teamselection must contain exactly 20 unique riders")
    return rider_ids


def analyze() -> dict[str, Any]:
    snapshot = load_snapshot("vuelta2026")
    predictions = json.loads(PREDICTIONS_PATH.read_text(encoding="utf-8"))
    projection = json.loads(PROJECTION_PATH.read_text(encoding="utf-8"))
    objective_scores_by_stage = {
        int(stage_no): {
            name_key(row["rider"]): float(row.get("score") or 0.0)
            for row in rows
        }
        for stage_no, rows in projection.get("stage_rankings", {}).items()
    }
    rider_ids = _personal_ids()
    riders = [snapshot.rider(rider_id) for rider_id in rider_ids]
    if any(rider is None for rider in riders):
        raise RuntimeError("personal team contains a rider absent from the live market")

    live_by_key = {name_key(rider.name): rider for rider in snapshot.riders}
    personal_by_key = {name_key(rider.name): rider for rider in riders}
    decisions = {
        name_key(row["rider"]): row for row in projection.get("decision_review", [])
    }
    price = sum(rider.price for rider in riders)
    team_counts = Counter(rider.team_id for rider in riders)
    unavailable = [rider.name for rider in riders if getattr(rider, "status", 1) != 1]
    legal = (
        price <= snapshot.budget
        and max(team_counts.values(), default=0) <= MAX_RIDERS_PER_TEAM
        and not unavailable
    )

    red_rider = live_by_key.get(name_key(PROJECTED_RED_JERSEY_RIDER))
    red_team_id = red_rider.team_id if red_rider is not None else None
    contributions = {
        key: {
            "rider": rider.name,
            "price": rider.price,
            "team_id": rider.team_id,
            "top20_appearances": 0,
            "selected_stages": 0,
            "stage_finish_points": 0.0,
            "team_bonus_points": 0.0,
            "red_jersey_team_bonus_points": 0.0,
            "captain_bonus_points": 0.0,
        }
        for key, rider in personal_by_key.items()
    }

    stages = []
    ideal_individual_ceiling = 0.0
    for stage in predictions["stages"]:
        rows_by_key = {name_key(row["rider"]): row for row in stage["top_20"]}
        objective_scores = objective_scores_by_stage.get(
            int(stage["stage_no"]), {}
        )
        winner = next(
            row for row in stage["top_20"] if int(row["predicted_finish"]) == 1
        )
        winner_rider = live_by_key.get(name_key(winner["rider"]))
        winner_team_id = winner_rider.team_id if winner_rider is not None else None
        candidates = []
        for key, rider in personal_by_key.items():
            predicted = rows_by_key.get(key)
            rank = int(predicted["predicted_finish"]) if predicted else None
            points = _rank_points(rank)
            team_points = (
                STAGE_WIN_TEAM_BONUS
                if winner_team_id is not None and rider.team_id == winner_team_id
                else 0.0
            )
            red_points = (
                RED_JERSEY_TEAM_BONUS
                if red_team_id is not None and rider.team_id == red_team_id
                else 0.0
            )
            if rank is not None:
                contributions[key]["top20_appearances"] += 1
            candidates.append(
                (
                    points + team_points + red_points,
                    points,
                    rank or 999,
                    rider.name,
                    key,
                    rider.team_id,
                    objective_scores.get(key, 0.0),
                )
            )
        candidates.sort(key=_lineup_sort_key)
        selected = candidates[:LINEUP_SIZE]
        captain = selected[0]
        if captain[1] <= 0:
            raise RuntimeError(f"stage {stage['stage_no']} has no scoring captain candidate")

        finish_points = sum(row[1] for row in selected)
        team_bonus = sum(
            STAGE_WIN_TEAM_BONUS for row in selected
            if winner_team_id is not None and row[5] == winner_team_id
        )
        red_bonus = sum(
            RED_JERSEY_TEAM_BONUS for row in selected
            if red_team_id is not None and row[5] == red_team_id
        )
        captain_team_bonus = (
            STAGE_WIN_TEAM_BONUS
            if winner_team_id is not None and captain[5] == winner_team_id else 0.0
        )
        captain_red_bonus = (
            RED_JERSEY_TEAM_BONUS
            if red_team_id is not None and captain[5] == red_team_id else 0.0
        )
        captain_bonus = (snapshot.captain_factor - 1) * (
            captain[1] + captain_team_bonus + captain_red_bonus
        )
        total = finish_points + team_bonus + red_bonus + captain_bonus

        for row in selected:
            contribution = contributions[row[4]]
            contribution["selected_stages"] += 1
            contribution["stage_finish_points"] += row[1]
            if winner_team_id is not None and row[5] == winner_team_id:
                contribution["team_bonus_points"] += STAGE_WIN_TEAM_BONUS
            if red_team_id is not None and row[5] == red_team_id:
                contribution["red_jersey_team_bonus_points"] += RED_JERSEY_TEAM_BONUS
        contributions[captain[4]]["captain_bonus_points"] += captain_bonus

        ideal_finish = sum(_rank_points(rank) for rank in range(1, LINEUP_SIZE + 1))
        ideal_captain = (snapshot.captain_factor - 1) * _rank_points(1)
        ideal_individual_ceiling += ideal_finish + ideal_captain
        stages.append(
            {
                "stage_no": int(stage["stage_no"]),
                "profile_type": stage.get("profile_type"),
                "finish_type": stage.get("finish_type"),
                "projected_winner": winner["rider"],
                "captain": captain[3],
                "captain_predicted_finish": (
                    None if captain[2] == 999 else captain[2]
                ),
                "lineup": [row[3] for row in selected],
                "scoring_riders": sum(row[1] > 0 for row in selected),
                "finish_points": round(finish_points, 2),
                "team_bonus_points": round(team_bonus, 2),
                "red_jersey_team_bonus_points": round(red_bonus, 2),
                "captain_bonus_points": round(captain_bonus, 2),
                "stage_total": round(total, 2),
            }
        )

    rider_rows = []
    for key, contribution in contributions.items():
        classification = float(decisions.get(key, {}).get("classification_jersey_points") or 0.0)
        contribution["classification_jersey_points"] = classification
        contribution["total_projected_contribution"] = sum(
            float(contribution[field])
            for field in (
                "stage_finish_points",
                "team_bonus_points",
                "red_jersey_team_bonus_points",
                "captain_bonus_points",
                "classification_jersey_points",
            )
        )
        for field, value in list(contribution.items()):
            if isinstance(value, float):
                contribution[field] = round(value, 2)
        rider_rows.append(contribution)
    rider_rows.sort(key=lambda row: row["total_projected_contribution"], reverse=True)
    for index, row in enumerate(rider_rows, start=1):
        row["contribution_rank"] = index
        if index <= 5:
            row["assessment"] = "excellent"
        elif index <= 10:
            row["assessment"] = "good"
        elif row["top20_appearances"] <= 2 and row["classification_jersey_points"] < 5:
            row["assessment"] = "doubtful"
        else:
            row["assessment"] = "borderline"

    finish_total = sum(stage["finish_points"] for stage in stages)
    team_bonus_total = sum(stage["team_bonus_points"] for stage in stages)
    red_bonus_total = sum(stage["red_jersey_team_bonus_points"] for stage in stages)
    captain_bonus_total = sum(stage["captain_bonus_points"] for stage in stages)
    classification_total = sum(row["classification_jersey_points"] for row in rider_rows)
    final_total = finish_total + team_bonus_total + red_bonus_total + captain_bonus_total + classification_total
    individual_total = finish_total + captain_bonus_total
    efficiency = individual_total / ideal_individual_ceiling
    grade = "A" if efficiency >= 0.75 else "B" if efficiency >= 0.65 else "C" if efficiency >= 0.55 else "D"

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "market_snapshot_time": datetime.fromtimestamp(
            (DATA_DIR / "_manifest.json").stat().st_mtime, tz=UTC
        ).isoformat(),
        "personal_snapshot_time": datetime.fromtimestamp(PERSONAL_PATH.stat().st_mtime, tz=UTC).isoformat(),
        "prediction_generated_at": predictions.get("generated_at"),
        "projection_generated_at": projection.get("generated_at"),
        "market_id": snapshot.market_id,
        "market_status": "live",
        "budget": snapshot.budget,
        "price": price,
        "budget_remaining": snapshot.budget - price,
        "legal": legal,
        "unique_riders": len(set(rider_ids)),
        "max_trade_team_count": max(team_counts.values(), default=0),
        "unavailable_riders": unavailable,
        "scoring_assumptions": {
            "stage_finish": "Predicted top-20 finish mapped through the exact Scorito rank table.",
            "lineup": "Best nine personal riders by predicted finish points; highest scorer captained.",
            "captain": "Captain doubles all projected stage entries, including conditional team and red-jersey-team bonuses.",
            "team_bonus": "+10 per enrolled teammate of the projected stage winner.",
            "red_jersey_team_bonus": "+8 per enrolled UAE teammate, assuming Tadej Pogacar holds red after every stage.",
            "classification": (
                "Separate whole-race classification/jersey proxy from the "
                f"{projection.get('model_version', 'current')} projection."
            ),
        },
        "data_gaps": [
            "Daily GC top-five, points, KOM and youth classification points cannot be derived from stage top-20 finishes and are represented only by the separate whole-race proxy.",
            "Projected team and red-jersey-team bonuses are conditional scenarios and do not drive lineup selection.",
        ],
        "totals": {
            "finish_points": round(finish_total, 2),
            "captain_bonus_points": round(captain_bonus_total, 2),
            "individual_stage_points": round(individual_total, 2),
            "team_bonus_points": round(team_bonus_total, 2),
            "red_jersey_team_bonus_points": round(red_bonus_total, 2),
            "classification_jersey_points": round(classification_total, 2),
            "final_projected_score": round(final_total, 2),
            "ideal_top20_individual_ceiling": round(ideal_individual_ceiling, 2),
            "top20_individual_efficiency": round(efficiency, 4),
            "team_grade": grade,
        },
        "riders": rider_rows,
        "stages": stages,
    }


def write_outputs(report: dict[str, Any]) -> None:
    OUTPUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with STAGES_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "stage_no", "profile_type", "finish_type", "projected_winner", "captain",
            "captain_predicted_finish", "scoring_riders", "finish_points", "team_bonus_points",
            "red_jersey_team_bonus_points", "captain_bonus_points", "stage_total", "lineup",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for stage in report["stages"]:
            writer.writerow({**stage, "lineup": "; ".join(stage["lineup"])})
    with RIDERS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        fields = [
            "contribution_rank", "assessment", "rider", "price", "top20_appearances",
            "selected_stages", "stage_finish_points", "captain_bonus_points", "team_bonus_points",
            "red_jersey_team_bonus_points", "classification_jersey_points",
            "total_projected_contribution",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(report["riders"])


def main() -> None:
    report = analyze()
    write_outputs(report)
    totals = report["totals"]
    print(
        f"Personal team: {totals['final_projected_score']:.2f} points, grade {totals['team_grade']}, "
        f"top-20 individual efficiency {totals['top20_individual_efficiency']:.1%}."
    )
    print(
        f"Components: {totals['finish_points']:.2f} finish + {totals['captain_bonus_points']:.2f} captain + "
        f"{totals['team_bonus_points']:.2f} team + {totals['red_jersey_team_bonus_points']:.2f} red-team + "
        f"{totals['classification_jersey_points']:.2f} classification/jersey."
    )
    for row in report["riders"]:
        print(
            f"{row['contribution_rank']:2}. {row['rider']:<24} {row['assessment']:<10} "
            f"{row['total_projected_contribution']:7.2f} pts  top20={row['top20_appearances']:2}"
        )
    print(f"Details: {OUTPUT_PATH}")
    print(f"Stages: {STAGES_CSV_PATH}")
    print(f"Riders: {RIDERS_CSV_PATH}")


if __name__ == "__main__":
    main()
