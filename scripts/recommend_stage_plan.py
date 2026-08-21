"""Forward squad + per-stage lineup plan on the live market, steered by chat intel.

Unlike ``recommend_vuelta_live.py`` this needs no PCS projection: expected
per-stage points come from a :class:`StageScorer` fitted on a completed grand
tour, so it runs the moment a market snapshot exists. When
``data/scorito/<slug>/expert_chat_intel.json`` is present, each rider's
per-stage score is adjusted once by the bounded schema-v2 evidence signal
before the squad and the 21 lineups are chosen.

Usage:
    python scripts/recommend_stage_plan.py --slug vuelta2026 --train giro2026
    python scripts/recommend_stage_plan.py --no-intel      # model-only baseline
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scorito_agent.expert_chat import (  # noqa: E402
    CONSUMER_MAX_ADJUSTMENT,
    apply_signal,
    name_key,
    signals_by_rider_stage,
)
from scorito_agent.scorito import (  # noqa: E402
    StageScorer,
    best_stage_lineup,
    joint_enrolled_squad,
    load_snapshot,
)

SQUAD_SIZE = 20
LINEUP_SIZE = 9
MAX_RIDERS_PER_TEAM = 4
MIN_SPRINT_OPTIONS = 5
TOP_PER_STAGE = 60
INTEL_MAX_ADJUST = CONSUMER_MAX_ADJUSTMENT
PCS_WEIGHT_DEFAULT = 0.5
_TRANSLITERATION = str.maketrans(
    {"\u00f8": "o", "\u00c6": "Ae", "\u00e6": "ae", "\u0142": "l", "\u0111": "d", "\u00df": "ss"}
)


def _slug(value: str) -> str:
    norm = unicodedata.normalize("NFKD", str(value).translate(_TRANSLITERATION))
    ascii_value = norm.encode("ascii", "ignore").decode("ascii").lower()
    return "-".join(re.findall(r"[a-z0-9]+", ascii_value))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(_slug(value).split("-"))


def _pcs_points(slug: str, snapshot) -> tuple[dict[tuple[int, int], float], str, int]:
    """``{(rider_id, stage_order) -> projected points}`` from the PCS evidence run."""
    path = ROOT / "data" / "scorito" / slug / "projected_recommendation.json"
    if not path.exists():
        return {}, "not available", 0
    projection = json.loads(path.read_text(encoding="utf-8"))
    rows = projection.get("riders", [])
    by_slug = {row["rider_slug"]: row for row in rows}
    tokens_by_slug = {row["rider_slug"]: _tokens(row["rider"]) for row in rows}
    slug_by_rider_id: dict[int, str] = {}
    for rider in snapshot.riders:
        direct = _slug(rider.name)
        if direct in by_slug:
            slug_by_rider_id[rider.rider_id] = direct
            continue
        live = _tokens(rider.name)
        hits = [
            candidate
            for candidate, projected in tokens_by_slug.items()
            if projected == live or projected <= live or live <= projected
        ]
        if len(hits) == 1:
            slug_by_rider_id[rider.rider_id] = hits[0]
    points: dict[tuple[int, int], float] = {}
    rankings = projection.get("stage_rankings", {})
    for stage in snapshot.stages:
        by_rider_slug = {
            row["rider_slug"]: float(row["projected_scorito_points"])
            for row in rankings.get(str(stage.order), [])
        }
        for rider_id, rider_slug in slug_by_rider_id.items():
            if rider_slug in by_rider_slug:
                points[(rider_id, stage.order)] = by_rider_slug[rider_slug]
    source = (
        f"projected_recommendation.json ({projection.get('generated_at', 'unknown time')}; "
        f"{projection.get('model_version', 'unknown model')})"
    )
    return points, source, len(slug_by_rider_id)


def _raw_riders(slug: str) -> dict[int, dict]:
    path = ROOT / "data" / "scorito" / slug / "eventriderenriched.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("Content", data) if isinstance(data, dict) else data
    return {int(row["RiderId"]): row for row in rows}


def _team_names(slug: str) -> dict[int, str]:
    path = ROOT / "data" / "scorito" / slug / "eventteam.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("Content", data) if isinstance(data, dict) else data
    return {int(row["Id"]): str(row.get("Name", row.get("ShortName", ""))) for row in rows}


def _load_intel(slug: str, stage_count: int) -> tuple[dict[str, dict[int, float]], str, dict]:
    path = ROOT / "data" / "scorito" / slug / "expert_chat_intel.json"
    if not path.exists():
        return {}, "not available - no expert-chat export ingested yet", {}
    digest = json.loads(path.read_text(encoding="utf-8"))
    summary = digest.get("summary", {})
    source = (
        f"{path.name} ({digest.get('generated_at', 'unknown time')}; "
        f"{summary.get('messages', 0)} messages, {len(digest.get('riders', {}))} riders)"
    )
    return signals_by_rider_stage(digest, stage_max=stage_count), source, digest


def build_plan(
    slug: str,
    train_slug: str,
    use_intel: bool = True,
    pcs_weight: float = PCS_WEIGHT_DEFAULT,
) -> dict:
    snapshot = load_snapshot(slug)
    raw = _raw_riders(slug)
    teams = _team_names(slug)
    scorer = StageScorer().fit(load_snapshot(train_slug))

    signals_by_key, intel_source, digest = (
        _load_intel(slug, len(snapshot.stages)) if use_intel else ({}, "disabled", {})
    )
    signals_by_rider = {
        rider.rider_id: signals_by_key[name_key(rider.name)]
        for rider in snapshot.riders
        if name_key(rider.name) in signals_by_key
        and any(abs(value) > 1e-9 for value in signals_by_key[name_key(rider.name)].values())
    }

    excluded = {
        rider_id for rider_id, row in raw.items() if int(row.get("Status", 1)) != 1
    }
    sprint_ids = {
        rider.rider_id for rider in snapshot.riders if rider.quality(3) >= 4
    }

    pcs_points, pcs_source, pcs_matched = _pcs_points(slug, snapshot)
    if not pcs_points:
        pcs_weight = 0.0
    stage_order = {stage.stage_id: stage.order for stage in snapshot.stages}
    base_points: dict[tuple[int, int], float] = {}
    tilted_points: dict[tuple[int, int], float] = {}
    for stage in snapshot.stages:
        candidates = [
            (rider.rider_id, float(scorer.expected(rider, stage)))
            for rider in snapshot.riders
            if rider.price > 0 and rider.rider_id not in excluded
        ]
        candidates = [(rider_id, value) for rider_id, value in candidates if value > 0]
        covered = [
            (rider_id, value)
            for rider_id, value in candidates
            if (rider_id, stage.order) in pcs_points
        ]
        # Put the PCS curve on the internal scorer's scale before blending.
        max_internal = max((value for _, value in covered), default=0.0)
        max_pcs = max(
            (pcs_points[(rider_id, stage.order)] for rider_id, _ in covered), default=0.0
        )
        scale = (max_internal / max_pcs) if max_pcs > 0 else 0.0
        scored = []
        for rider_id, base in candidates:
            pcs = pcs_points.get((rider_id, stage.order))
            if pcs is not None and scale > 0:
                base = (1.0 - pcs_weight) * base + pcs_weight * pcs * scale
            signal = signals_by_rider.get(rider_id, {}).get(stage.order, 0.0)
            scored.append((rider_id, base, apply_signal(base, signal)))
        scored.sort(key=lambda row: row[2], reverse=True)
        for rider_id, base, tilted in scored[:TOP_PER_STAGE]:
            base_points[(rider_id, stage.stage_id)] = base
            tilted_points[(rider_id, stage.stage_id)] = tilted

    def points_fn(rider_id: int, stage) -> float:
        return tilted_points.get((rider_id, stage.stage_id), 0.0)

    plan = joint_enrolled_squad(
        snapshot,
        points_fn,
        squad_size=SQUAD_SIZE,
        lineup_size=LINEUP_SIZE,
        max_riders_per_team=MAX_RIDERS_PER_TEAM,
        coverage_constraints=[(sprint_ids, MIN_SPRINT_OPTIONS)],
        excluded_rider_ids=excluded,
    )
    if plan is None:
        raise SystemExit("joint MILP failed (scipy missing or model infeasible)")

    lineups = []
    projected_total = 0.0
    for stage in snapshot.stages:
        stage_points = {
            rider_id: points_fn(rider_id, stage) for rider_id in plan.rider_ids
        }
        lineup = best_stage_lineup(
            stage,
            plan.rider_ids,
            stage_points,
            lineup_size=LINEUP_SIZE,
            captain_factor=snapshot.captain_factor,
        )
        projected_total += lineup.total
        lineups.append(
            {
                "stage_no": stage_order[stage.stage_id],
                "stage_type": stage.stage_type,
                "terrain": stage.terrain_type,
                "captain": snapshot.rider(lineup.captain_id).name,
                "captain_projected": round(lineup.captain_points, 2),
                "lineup": [snapshot.rider(rider_id).name for rider_id in lineup.rider_ids],
                "projected_stage_total": round(lineup.total, 2),
            }
        )

    squad = []
    for rider_id in plan.rider_ids:
        rider = snapshot.rider(rider_id)
        starts = sum(1 for row in lineups if rider.name in row["lineup"])
        captaincies = sum(1 for row in lineups if row["captain"] == rider.name)
        per_stage_signals = signals_by_rider.get(rider_id, {})
        squad.append(
            {
                "rider_id": rider_id,
                "rider": rider.name,
                "team": teams.get(rider.team_id, str(rider.team_id)),
                "role": rider.role_label,
                "price": rider.price,
                "price_m": round(rider.price / 1_000_000, 2),
                "projected_starts": starts,
                "captaincies": captaincies,
                "sprint_option": rider_id in sprint_ids,
                "intel_signal_avg": (
                    round(sum(per_stage_signals.values()) / len(per_stage_signals), 3)
                    if per_stage_signals
                    else 0.0
                ),
            }
        )
    squad.sort(key=lambda row: (-row["projected_starts"], -row["price"]))

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "slug": slug,
        "train_slug": train_slug,
        "value_source": (
            f"StageScorer fitted on {train_slug} (out-of-sample), live {slug} prices "
            f"and qualities; expert-chat intel capped at {INTEL_MAX_ADJUST * 100:.0f}%"
        ),
        "pcs_source": pcs_source,
        "pcs_weight": pcs_weight,
        "pcs_matched_riders": pcs_matched,
        "pcs_unmatched_riders": sorted(
            rider.name
            for rider in snapshot.riders
            if (rider.rider_id, snapshot.stages[0].order) not in pcs_points
        ),
        "intel_source": intel_source,
        "intel_riders": sorted(
            snapshot.rider(rider_id).name for rider_id in signals_by_rider
        ),
        "intel_stage_intent": digest.get("stage_intent", {}),
        "budget": snapshot.budget,
        "squad_price": plan.total_price,
        "budget_remaining": snapshot.budget - plan.total_price,
        "projected_objective": round(plan.value, 2),
        "projected_stage_total": round(projected_total, 2),
        "riders_considered": len(snapshot.riders) - len(excluded),
        "excluded_riders": sorted(
            snapshot.rider(rider_id).name
            for rider_id in excluded
            if snapshot.rider(rider_id)
        ),
        "squad": squad,
        "stage_plan": lineups,
    }


def _resolve_output_path(value: str, slug: str) -> Path:
    path = Path(value) if value else Path("data") / "scorito" / slug / "stage_plan.json"
    return path if path.is_absolute() else ROOT / path


def _display_output_path(path: Path) -> Path:
    try:
        return path.relative_to(ROOT)
    except ValueError:
        return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", default="vuelta2026")
    parser.add_argument("--train", default="giro2026")
    parser.add_argument("--no-intel", action="store_true")
    parser.add_argument("--pcs-weight", type=float, default=PCS_WEIGHT_DEFAULT)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    plan = build_plan(
        args.slug, args.train, use_intel=not args.no_intel, pcs_weight=args.pcs_weight
    )
    out_path = _resolve_output_path(args.out, args.slug)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"slug            : {plan['slug']} (scorer trained on {plan['train_slug']})")
    print(
        f"pcs blend       : {plan['pcs_weight']:.2f} weight, "
        f"{plan['pcs_matched_riders']}/{plan['riders_considered']} riders matched"
    )
    print(f"pcs source      : {plan['pcs_source']}")
    print(f"intel           : {plan['intel_source']}")
    print(f"budget          : {plan['squad_price'] / 1e6:.2f}M / {plan['budget'] / 1e6:.2f}M")
    print(f"projected total : {plan['projected_stage_total']:.0f} pts over {len(plan['stage_plan'])} stages")
    print("\nsquad:")
    for row in plan["squad"]:
        print(
            f"  {row['price_m']:>5.2f}M  {row['rider']:<26} {row['role']:<9} "
            f"starts {row['projected_starts']:>2}  capt {row['captaincies']:>2}  "
            f"intel {row['intel_signal_avg']:+.2f}"
        )
    print("\nstage plan:")
    for row in plan["stage_plan"]:
        print(
            f"  stage {row['stage_no']:>2}  captain {row['captain']:<24} "
            f"{row['projected_stage_total']:>7.1f} pts"
        )
    print(f"\nwritten: {_display_output_path(out_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
