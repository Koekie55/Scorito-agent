"""Score every saved Vuelta squad against the current per-stage top-20 forecast."""
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
SUMMARY_PATH = DATA_DIR / "saved_team_stage_prediction_scores.csv"
DETAIL_PATH = DATA_DIR / "saved_team_stage_prediction_scores.json"
LINEUPS_PATH = DATA_DIR / "saved_team_stage_prediction_lineups.csv"
SQUAD_SIZE = 20
LINEUP_SIZE = 9
MAX_RIDERS_PER_TEAM = 4


def name_token_key(name: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(char for char in normalized if not unicodedata.combining(char))
    return tuple(sorted(re.findall(r"[a-z0-9]+", ascii_name.lower())))


def load_saved_squads(data_dir: Path = DATA_DIR) -> list[dict[str, Any]]:
    candidates: list[tuple[str, str, list[str]]] = []
    scenario_files = [
        data_dir / "vuelta2026_live_prices_team_lineups.csv",
        data_dir / "vuelta2026_personal_team_lineups.csv",
    ]
    for path in scenario_files:
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("record_type") != "scenario":
                    continue
                riders = [name.strip() for name in row["scenario_squad"].split(";") if name.strip()]
                candidates.append((str(row["scenario"]), path.name, riders))

    projected_files = [
        "vuelta2026_projected_squad.csv",
        "vuelta2026_projected_squad_evidence_v4.csv",
        "vuelta2026_projected_squad_evidence_v3.csv",
        "vuelta2026_projected_squad_recalibrated.csv",
    ]
    for filename in projected_files:
        path = data_dir / filename
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        riders = [str(row.get("rider") or row.get("Rider") or "").strip() for row in rows]
        candidates.append((path.stem, path.name, [name for name in riders if name]))

    deduplicated: dict[tuple[tuple[str, ...], ...], dict[str, Any]] = {}
    for name, source, riders in candidates:
        if len(riders) != SQUAD_SIZE or len(set(map(name_token_key, riders))) != SQUAD_SIZE:
            raise RuntimeError(f"{source}:{name} is not a 20-rider unique squad")
        signature = tuple(sorted(name_token_key(rider) for rider in riders))
        if signature in deduplicated:
            deduplicated[signature]["sources"].append(source)
            continue
        deduplicated[signature] = {"team": name, "sources": [source], "riders": riders}
    return list(deduplicated.values())


def _rank_points(rank: int | None) -> float:
    return float(SCORITO_STAGE_POINTS.get(rank or 0, 0.0))


def score_saved_squads(
    predictions: dict[str, Any],
    projection: dict[str, Any],
    squads: list[dict[str, Any]],
    snapshot: Any,
) -> dict[str, Any]:
    projection_riders = {
        name_token_key(row["rider"]): row for row in projection.get("riders", [])
    }
    classification = {
        name_token_key(row["rider"]): float(row.get("classification_jersey_points") or 0.0)
        for row in projection.get("decision_review", [])
    }
    live_riders = {name_token_key(rider.name): rider for rider in snapshot.riders}
    stage_rows = []
    global_stage_ceiling = 0.0
    for stage in predictions["stages"]:
        rank_by_key = {
            name_token_key(row["rider"]): int(row["predicted_finish"])
            for row in stage["top_20"]
        }
        ideal_points = [_rank_points(rank) for rank in range(1, LINEUP_SIZE + 1)]
        ideal_total = sum(ideal_points) + (snapshot.captain_factor - 1) * ideal_points[0]
        global_stage_ceiling += ideal_total
        stage_rows.append((stage, rank_by_key, ideal_total))

    scored_teams = []
    for squad in squads:
        keys = [name_token_key(name) for name in squad["riders"]]
        matched_projection = [key for key in keys if key in projection_riders]
        unmatched_prediction = [
            name for name, key in zip(squad["riders"], keys, strict=True)
            if key not in projection_riders
        ]
        unmatched_market = [
            name for name, key in zip(squad["riders"], keys, strict=True)
            if key not in live_riders
        ]
        live_members = [live_riders[key] for key in keys if key in live_riders]
        price = sum(rider.price for rider in live_members)
        team_counts = Counter(rider.team_id for rider in live_members)
        legal = (
            len(keys) == SQUAD_SIZE
            and len(set(keys)) == SQUAD_SIZE
            and not unmatched_market
            and price <= snapshot.budget
            and max(team_counts.values(), default=0) <= MAX_RIDERS_PER_TEAM
        )

        lineups = []
        stage_total = 0.0
        top20_appearances = 0
        scoring_rider_stages = 0
        for stage, rank_by_key, ideal_total in stage_rows:
            candidates = []
            for saved_name, key in zip(squad["riders"], keys, strict=True):
                rank = rank_by_key.get(key)
                points = _rank_points(rank)
                canonical = projection_riders.get(key, {}).get("rider", saved_name)
                candidates.append((points, rank or 999, canonical, key))
                if rank is not None:
                    top20_appearances += 1
            candidates.sort(key=lambda row: (-row[0], row[1], row[2]))
            selected = candidates[:LINEUP_SIZE]
            captain = selected[0]
            total = sum(row[0] for row in selected)
            total += (snapshot.captain_factor - 1) * captain[0]
            stage_total += total
            scoring_rider_stages += sum(row[0] > 0 for row in selected)
            lineups.append(
                {
                    "stage_no": int(stage["stage_no"]),
                    "profile_type": stage.get("profile_type"),
                    "lineup": [row[2] for row in selected],
                    "captain": captain[2],
                    "captain_predicted_finish": None if captain[1] == 999 else captain[1],
                    "scoring_riders": sum(row[0] > 0 for row in selected),
                    "projected_stage_points": round(total, 2),
                    "ideal_field_points": round(ideal_total, 2),
                }
            )

        classification_total = sum(classification.get(key, 0.0) for key in keys)
        final_total = stage_total + classification_total
        scored_teams.append(
            {
                "team": squad["team"],
                "sources": squad["sources"],
                "riders": squad["riders"],
                "legal_current_market": legal,
                "current_price": price if not unmatched_market else None,
                "budget_remaining": snapshot.budget - price if not unmatched_market else None,
                "max_trade_team_count": max(team_counts.values(), default=0),
                "prediction_field_riders": len(matched_projection),
                "unmatched_prediction_riders": unmatched_prediction,
                "unmatched_market_riders": unmatched_market,
                "top20_appearances": top20_appearances,
                "scoring_lineup_slots": scoring_rider_stages,
                "projected_enrolled_stage_points": round(stage_total, 2),
                "projected_classification_jersey_points": round(classification_total, 2),
                "final_projected_point_score": round(final_total, 2),
                "stage_ceiling_efficiency": round(stage_total / global_stage_ceiling, 4),
                "lineups": lineups,
            }
        )

    scored_teams.sort(key=lambda row: row["final_projected_point_score"], reverse=True)
    for rank, team in enumerate(scored_teams, start=1):
        team["rank"] = rank
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "prediction_generated_at": predictions.get("generated_at"),
        "prediction_field_count": predictions.get("known_pcs_participants"),
        "market_id": snapshot.market_id,
        "market_rider_count": len(snapshot.riders),
        "market_budget": snapshot.budget,
        "captain_factor": snapshot.captain_factor,
        "scoring_method": (
            "Predicted top-20 rank mapped through the exact Scorito points table; "
            "best nine per saved squad; highest projected scorer captained. Riders outside a stage top 20 score zero."
        ),
        "classification_method": (
            "Separate PCS projection classification/jersey estimate; unmatched prediction riders receive zero."
        ),
        "global_ideal_stage_ceiling": round(global_stage_ceiling, 2),
        "teams": scored_teams,
    }


def write_outputs(report: dict[str, Any]) -> None:
    DETAIL_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_fields = [
        "rank", "team", "sources", "legal_current_market", "current_price_m",
        "budget_remaining_m", "max_trade_team_count", "prediction_field_riders",
        "unmatched_prediction_riders", "unmatched_market_riders", "top20_appearances",
        "scoring_lineup_slots", "projected_enrolled_stage_points",
        "projected_classification_jersey_points", "final_projected_point_score",
        "stage_ceiling_efficiency",
    ]
    with SUMMARY_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=summary_fields)
        writer.writeheader()
        for team in report["teams"]:
            writer.writerow(
                {
                    **{key: team.get(key) for key in summary_fields},
                    "sources": "; ".join(team["sources"]),
                    "current_price_m": (
                        f"{team['current_price'] / 1_000_000:.2f}" if team["current_price"] is not None else ""
                    ),
                    "budget_remaining_m": (
                        f"{team['budget_remaining'] / 1_000_000:.2f}"
                        if team["budget_remaining"] is not None else ""
                    ),
                    "unmatched_prediction_riders": "; ".join(team["unmatched_prediction_riders"]),
                    "unmatched_market_riders": "; ".join(team["unmatched_market_riders"]),
                }
            )

    lineup_fields = [
        "team_rank", "team", "stage_no", "profile_type", "captain",
        "captain_predicted_finish", "scoring_riders", "projected_stage_points",
        "ideal_field_points", "lineup",
    ]
    with LINEUPS_PATH.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=lineup_fields)
        writer.writeheader()
        for team in report["teams"]:
            for lineup in team["lineups"]:
                writer.writerow(
                    {
                        **{key: lineup.get(key) for key in lineup_fields},
                        "team_rank": team["rank"],
                        "team": team["team"],
                        "lineup": "; ".join(lineup["lineup"]),
                    }
                )


def main() -> None:
    predictions = json.loads(PREDICTIONS_PATH.read_text(encoding="utf-8"))
    projection = json.loads(PROJECTION_PATH.read_text(encoding="utf-8"))
    snapshot = load_snapshot("vuelta2026")
    report = score_saved_squads(predictions, projection, load_saved_squads(), snapshot)
    write_outputs(report)
    print(
        f"Scored {len(report['teams'])} unique saved squads against "
        f"{len(predictions['stages'])} stages ({report['prediction_field_count']} PCS riders)."
    )
    for team in report["teams"]:
        print(
            f"{team['rank']}. {team['team']}: {team['final_projected_point_score']:.2f} "
            f"({team['projected_enrolled_stage_points']:.2f} stage + "
            f"{team['projected_classification_jersey_points']:.2f} classification), "
            f"field={team['prediction_field_riders']}/20, legal={team['legal_current_market']}"
        )
    print(f"Summary: {SUMMARY_PATH}")
    print(f"Details: {DETAIL_PATH}")
    print(f"Lineups: {LINEUPS_PATH}")


if __name__ == "__main__":
    main()
