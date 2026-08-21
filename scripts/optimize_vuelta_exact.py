"""Optimize Vuelta squads with one consistent Scorito scoring engine."""
from __future__ import annotations

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

from scorito_agent.scorito import joint_enrolled_squad, load_snapshot  # noqa: E402
from scripts.refresh_vuelta_stage_predictions import SCORITO_STAGE_POINTS  # noqa: E402

DATA_DIR = ROOT / "data" / "scorito" / "vuelta2026"
PREDICTIONS_PATH = DATA_DIR / "stage_top20_predictions.json"
PROJECTION_PATH = DATA_DIR / "projected_recommendation.json"
EXPERT_PATH = DATA_DIR / "qk_expert_opinion.json"
PERSONAL_PATH = DATA_DIR / "personal" / "teamselection.json"
OUTPUT_PATH = DATA_DIR / "optimal_team_exact_analysis.json"
SQUAD_SIZE = 20
LINEUP_SIZE = 9
MAX_RIDERS_PER_TEAM = 4
STAGE_WIN_TEAM_BONUS = 10.0
RED_JERSEY_TEAM_BONUS = 8.0
PROJECTED_RED_JERSEY_RIDER = "Tadej Pogacar"


def name_key(name: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKD", str(name))
    ascii_name = "".join(
        character for character in normalized
        if not unicodedata.combining(character)
    )
    return tuple(sorted(re.findall(r"[a-z0-9]+", ascii_name.lower())))


def _rank_points(rank: int | None) -> float:
    return float(SCORITO_STAGE_POINTS.get(rank or 0, 0.0))


def _content(path: Path) -> Any:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("Content", payload) if isinstance(payload, dict) else payload


def _unavailable_slugs(projection: dict[str, Any]) -> set[str]:
    unavailable = set(projection.get("constraints", {}).get("unavailable_riders", []))
    if EXPERT_PATH.exists():
        expert = json.loads(EXPERT_PATH.read_text(encoding="utf-8"))
        unavailable.update(
            str(row.get("rider_slug") or "")
            for row in expert.get("selection_assumptions", {}).get(
                "unavailable_riders", []
            )
            if row.get("rider_slug")
        )
    return unavailable


def _scenario_inputs() -> dict[str, Any]:
    snapshot = load_snapshot("vuelta2026")
    predictions = json.loads(PREDICTIONS_PATH.read_text(encoding="utf-8"))
    projection = json.loads(PROJECTION_PATH.read_text(encoding="utf-8"))
    live_by_key = {name_key(rider.name): rider for rider in snapshot.riders}
    slug_by_id = {
        live_by_key[name_key(row["rider"])].rider_id: str(row["rider_slug"])
        for row in projection.get("riders", [])
        if name_key(row["rider"]) in live_by_key
    }
    classification_by_key = {
        name_key(row["rider"]): float(row.get("classification_jersey_points") or 0.0)
        for row in projection.get("decision_review", [])
    }
    classification = {
        rider.rider_id: classification_by_key.get(name_key(rider.name), 0.0)
        for rider in snapshot.riders
    }

    red_rider = live_by_key[name_key(PROJECTED_RED_JERSEY_RIDER)]
    red_team_id = red_rider.team_id
    stages_by_order = {stage.order: stage for stage in snapshot.stages}
    base_points: dict[tuple[int, int], float] = {}
    conditional_points: dict[tuple[int, int], float] = {}
    stage_context: dict[int, dict[str, Any]] = {}
    for predicted_stage in predictions["stages"]:
        order = int(predicted_stage["stage_no"])
        stage = stages_by_order[order]
        rank_by_key = {
            name_key(row["rider"]): int(row["predicted_finish"])
            for row in predicted_stage["top_20"]
        }
        winner_row = next(
            row for row in predicted_stage["top_20"]
            if int(row["predicted_finish"]) == 1
        )
        winner = live_by_key[name_key(winner_row["rider"])]
        stage_context[stage.stage_id] = {
            "stage_no": order,
            "profile_type": predicted_stage.get("profile_type"),
            "winner": winner.name,
            "winner_team_id": winner.team_id,
        }
        for rider in snapshot.riders:
            finish = _rank_points(rank_by_key.get(name_key(rider.name)))
            team = (
                STAGE_WIN_TEAM_BONUS if rider.team_id == winner.team_id else 0.0
            )
            red = RED_JERSEY_TEAM_BONUS if rider.team_id == red_team_id else 0.0
            base_points[(rider.rider_id, stage.stage_id)] = finish
            conditional_points[(rider.rider_id, stage.stage_id)] = finish + team + red

    raw_by_id = {
        int(row["RiderId"]): row
        for row in _content(DATA_DIR / "eventriderenriched.json")
    }
    unavailable = _unavailable_slugs(projection)
    excluded_ids = {
        rider.rider_id
        for rider in snapshot.riders
        if int(raw_by_id.get(rider.rider_id, {}).get("Status", 0)) != 1
        or slug_by_id.get(rider.rider_id) in unavailable
        or rider.rider_id not in slug_by_id
    }
    sprint_ids = {
        live_by_key[name_key(row["rider"])].rider_id
        for row in projection.get("riders", [])
        if name_key(row["rider"]) in live_by_key
        and row.get("capabilities", {}).get("sprint_assessment", {}).get("eligible") is True
    }
    minimum_sprints = int(
        projection.get("constraints", {}).get("minimum_credible_sprint_options", 5)
    )
    personal_payload = json.loads(PERSONAL_PATH.read_text(encoding="utf-8"))
    personal_ids = list(personal_payload.get("Content", personal_payload))
    return {
        "snapshot": snapshot,
        "predictions": predictions,
        "projection": projection,
        "classification": classification,
        "base_points": base_points,
        "conditional_points": conditional_points,
        "stage_context": stage_context,
        "excluded_ids": excluded_ids,
        "sprint_ids": sprint_ids,
        "minimum_sprints": minimum_sprints,
        "personal_ids": personal_ids,
        "red_team_id": red_team_id,
        "unavailable_slugs": sorted(unavailable),
    }


def _score_fixed_squad(
    inputs: dict[str, Any], rider_ids: list[int], *, conditional: bool
) -> dict[str, Any]:
    snapshot = inputs["snapshot"]
    points = inputs["conditional_points"] if conditional else inputs["base_points"]
    stages = []
    enrolled_total = 0.0
    finish_total = 0.0
    team_total = 0.0
    red_total = 0.0
    captain_total = 0.0
    for stage in snapshot.stages:
        context = inputs["stage_context"][stage.stage_id]
        ranked = sorted(
            rider_ids,
            key=lambda rider_id: (-points[(rider_id, stage.stage_id)], rider_id),
        )
        selected = ranked[:LINEUP_SIZE]
        captain_id = selected[0]
        finish = sum(inputs["base_points"][(rid, stage.stage_id)] for rid in selected)
        team = sum(
            STAGE_WIN_TEAM_BONUS
            for rid in selected
            if snapshot.rider(rid).team_id == context["winner_team_id"]
        ) if conditional else 0.0
        red = sum(
            RED_JERSEY_TEAM_BONUS
            for rid in selected
            if snapshot.rider(rid).team_id == inputs["red_team_id"]
        ) if conditional else 0.0
        captain = points[(captain_id, stage.stage_id)]
        total = finish + team + red + captain
        finish_total += finish
        team_total += team
        red_total += red
        captain_total += captain
        enrolled_total += total
        stages.append(
            {
                "stage_no": context["stage_no"],
                "profile_type": context["profile_type"],
                "winner": context["winner"],
                "lineup": [snapshot.rider(rid).name for rid in selected],
                "captain": snapshot.rider(captain_id).name,
                "finish_points": finish,
                "team_bonus_points": team,
                "red_jersey_team_bonus_points": red,
                "captain_bonus_points": captain,
                "stage_total": total,
            }
        )
    classification = sum(inputs["classification"].get(rid, 0.0) for rid in rider_ids)
    return {
        "rider_ids": rider_ids,
        "riders": [snapshot.rider(rid).name for rid in rider_ids],
        "price": sum(snapshot.rider(rid).price for rid in rider_ids),
        "finish_points": round(finish_total, 2),
        "captain_bonus_points": round(captain_total, 2),
        "team_bonus_points": round(team_total, 2),
        "red_jersey_team_bonus_points": round(red_total, 2),
        "classification_jersey_points": round(classification, 2),
        "final_projected_score": round(enrolled_total + classification, 2),
        "stages": stages,
    }


def _optimize(inputs: dict[str, Any], *, conditional: bool) -> dict[str, Any]:
    snapshot = inputs["snapshot"]
    points = inputs["conditional_points"] if conditional else inputs["base_points"]
    plan = joint_enrolled_squad(
        snapshot,
        lambda rider_id, stage: points[(rider_id, stage.stage_id)],
        budget=snapshot.budget,
        squad_size=SQUAD_SIZE,
        lineup_size=LINEUP_SIZE,
        selection_values=inputs["classification"],
        max_riders_per_team=MAX_RIDERS_PER_TEAM,
        coverage_constraints=[(inputs["sprint_ids"], inputs["minimum_sprints"])],
        excluded_rider_ids=inputs["excluded_ids"],
    )
    if plan is None:
        raise RuntimeError("exact-scoring squad optimizer is infeasible")
    return _score_fixed_squad(inputs, list(plan.rider_ids), conditional=conditional)


def build_report() -> dict[str, Any]:
    inputs = _scenario_inputs()
    base_optimal = _optimize(inputs, conditional=False)
    conditional_optimal = _optimize(inputs, conditional=True)
    personal_base = _score_fixed_squad(
        inputs, inputs["personal_ids"], conditional=False
    )
    personal_conditional = _score_fixed_squad(
        inputs, inputs["personal_ids"], conditional=True
    )
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "prediction_generated_at": inputs["predictions"].get("generated_at"),
        "projection_generated_at": inputs["projection"].get("generated_at"),
        "scoring_method": {
            "finish": "Exact Scorito top-20 rank table.",
            "lineup": "Best nine by total projected stage points.",
            "captain": "Captain doubles the full stage sum, including team and jersey-team points.",
            "winner_team": "+10 for each enrolled rider on the projected winner's team.",
            "red_team": "+8 for each enrolled UAE rider, assuming Pogačar holds red after every stage.",
            "classification": "Evidence-v5 projection; daily GC/green/KOM/youth outcomes remain uncertain.",
        },
        "constraints": {
            "budget": inputs["snapshot"].budget,
            "squad_size": SQUAD_SIZE,
            "lineup_size": LINEUP_SIZE,
            "max_riders_per_team": MAX_RIDERS_PER_TEAM,
            "minimum_sprint_options": inputs["minimum_sprints"],
            "unavailable_riders": inputs["unavailable_slugs"],
        },
        "base_optimal": base_optimal,
        "conditional_optimal": conditional_optimal,
        "personal_base": personal_base,
        "personal_conditional": personal_conditional,
    }


def main() -> None:
    report = build_report()
    OUTPUT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name in (
        "base_optimal", "conditional_optimal", "personal_base", "personal_conditional"
    ):
        scenario = report[name]
        print(f"{name}: {scenario['final_projected_score']:.2f} points, {scenario['price'] / 1_000_000:.2f}M")
        print("  " + "; ".join(scenario["riders"]))
    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()