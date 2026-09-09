r"""Validate the PCS stage-similarity predictor by walk-forward testing.

This is the Part-3 "does the similarity model actually predict?" check. It is
the offline analogue of ``scripts/validate_predictor.py`` (Part 2), but instead
of an external cyclingoracle ranking it validates our OWN
:func:`scorito_agent.pcs.predict.predict_finishers` similarity predictor against
the realized Scorito points stored in the PCS corpus.

Corpus
------
``data/pcs/stages.json`` (built by ``scripts/build_pcs_store.py``) holds one
record per finished stage with ``profile_type`` / ``finish_type`` / ``startlist``
and a ``results`` list (``rider_slug`` + realized Scorito ``points``, rank-sorted).

Pre-race holdout protocol
-------------------------
For every stored stage that has results and a startlist:

1. Build a training pool from every completed Grand Tour strictly before the
    target race. No stage from the target race enters the pool because the
    production projection forecasts the full race before its opening stage.
2. Run ``predict_finishers(target, pool, top_n=200)`` to get a full predicted
   ranking of the target's participants.
3. Ground truth = ``{rider_slug -> realized points}`` from the target's own
   ``results``.  Riders predicted but absent from ``results`` scored 0.
4. Spearman-correlate the predictor's skill signal (``-predicted_rank``) against
   realized points over the predicted riders.  Report per-stage rho and two
   pooled aggregates (macro-average and a within-stage realized-rank pooled rho),
   plus a top-10 hit-rate (how many of the predicted top-10 land in the realized
   top-10).

Matching is EXACT on ``rider_slug`` because both the predictions and the ground
truth are derived from the same store (no accent/name reconciliation needed).

Run:
    $env:PYTHONPATH="src"; $env:PYTHONUTF8="1"; \
        .\.venv\Scripts\python.exe scripts\validate_pcs_predictor.py [race]
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

# Make ``src`` importable when run as a plain script.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scorito_agent.pcs.predict import predict_finishers  # noqa: E402
from scorito_agent.pcs.store import StageStore  # noqa: E402

TOP_N = 200  # predict a full ranking for Spearman coverage
TOP_HIT = 10  # top-k hit-rate window
PROTOCOL_VERSION = 2
EVALUATION_MODE = "pre-race cross-race holdout"
RACE_CALENDAR_ORDER = {"giro": 1, "tdf": 2, "tour": 2, "vuelta": 3}


# ---------------------------------------------------------------------------
# Spearman (pure-python, tie-aware, degenerate-safe)
# ---------------------------------------------------------------------------

def _rankdata(values: list[float]) -> list[float]:
    """Average-rank transform (ties share the mean rank)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    n = len(values)
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0  # 1-based average rank
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(x: list[float], y: list[float]) -> float | None:
    n = len(x)
    if n < 2:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0.0 or syy <= 0.0:
        return None  # degenerate / constant
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return sxy / math.sqrt(sxx * syy)


def spearman(x: list[float], y: list[float]) -> float | None:
    """Spearman rho, tie-aware, degenerate-safe."""
    if len(x) < 3:
        return None
    return _pearson(_rankdata(x), _rankdata(y))


# ---------------------------------------------------------------------------
# ground-truth helpers
# ---------------------------------------------------------------------------

def _points_map(stage: dict[str, Any]) -> dict[str, float]:
    """``{rider_slug -> realized Scorito points}`` from a stage's results."""
    out: dict[str, float] = {}
    for res in stage.get("results") or []:
        slug = res.get("rider_slug") or res.get("slug")
        if not slug:
            continue
        try:
            out[slug] = float(res.get("points") or 0.0)
        except (TypeError, ValueError):
            out[slug] = 0.0
    return out


def _realized_top(stage: dict[str, Any], k: int) -> list[str]:
    """The realized top-k rider slugs by points (points > 0 only)."""
    pm = _points_map(stage)
    scoring = [(slug, pts) for slug, pts in pm.items() if pts > 0]
    scoring.sort(key=lambda kv: kv[1], reverse=True)
    return [slug for slug, _ in scoring[:k]]


def _is_before_target(candidate: dict[str, Any], target: dict[str, Any]) -> bool:
    """Return whether a same-race candidate is chronologically before target."""
    try:
        candidate_no = int(candidate.get("stage_no"))
        target_no = int(target.get("stage_no"))
    except (TypeError, ValueError):
        candidate_no = target_no = None
    if candidate_no is not None and target_no is not None:
        return candidate_no < target_no

    try:
        candidate_date = date.fromisoformat(str(candidate.get("date")))
        target_date = date.fromisoformat(str(target.get("date")))
    except ValueError:
        return False
    return candidate_date < target_date


def _race_family(stage: dict[str, Any]) -> str | None:
    race = str(stage.get("race") or "").strip().lower()
    return next(
        (family for family in RACE_CALENDAR_ORDER if race.startswith(family)),
        None,
    )


def _production_training_pool(
    all_stages: list[dict[str, Any]], target: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the completed races available before a pre-race projection."""
    target_family = _race_family(target)
    try:
        target_year = int(target.get("year"))
    except (TypeError, ValueError):
        return []
    if target_family is None:
        return []

    target_key = (target_year, RACE_CALENDAR_ORDER[target_family])
    pool: list[dict[str, Any]] = []
    for candidate in all_stages:
        candidate_family = _race_family(candidate)
        try:
            candidate_year = int(candidate.get("year"))
        except (TypeError, ValueError):
            continue
        if candidate_family is None:
            continue
        candidate_key = (candidate_year, RACE_CALENDAR_ORDER[candidate_family])
        if candidate_key < target_key and candidate.get("results"):
            pool.append(candidate)
    return pool


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    race_filter = sys.argv[1].strip().lower() if len(sys.argv) > 1 else None

    store = StageStore()
    all_stages = store.all_stages()
    if not all_stages:
        print(f"[error] no stages in {store.path}", file=sys.stderr)
        print("        run scripts/build_pcs_store.py first.", file=sys.stderr)
        return 2

    by_race: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for st in all_stages:
        by_race[str(st.get("race") or "unknown")].append(st)

    races = sorted(by_race)
    print(
        f"Loaded {len(all_stages)} stages across {len(races)} races "
        f"from {store.path.name}: "
        + ", ".join(f"{r}={len(by_race[r])}" for r in races)
    )
    if race_filter:
        print(f"Filtering to race = {race_filter!r}")

    print("\nPre-race cross-race holdout correlation "
          "(predicted rank vs realized Scorito points)")
    print(f"{'race':>8}  {'st':>3}  {'profile':>8}  {'finish':>7}  "
          f"{'pred':>4}  {'rho':>7}  {'top10':>5}  note")
    print("-" * 78)

    per_stage_rho: list[float] = []
    top_hit_fracs: list[float] = []
    pooled_pred_rank: list[float] = []
    pooled_realized_rank: list[float] = []
    evaluated = 0

    for race in races:
        if race_filter and race != race_filter:
            continue
        stages = by_race[race]
        for target in sorted(stages, key=lambda s: s.get("stage_no") or 0):
            if not target.get("results") or not (
                target.get("startlist") or target.get("participants")
            ):
                continue
            stage_no = target.get("stage_no")
            pool = _production_training_pool(all_stages, target)
            if not pool:
                continue

            out = predict_finishers(target, pool, top_n=TOP_N)
            preds = out.get("predictions") or []
            evaluated += 1

            pm = _points_map(target)
            skill: list[float] = []     # -predicted_rank (higher = predicted better)
            realized: list[float] = []  # realized Scorito points
            for pred in preds:
                slug = pred.get("rider_slug")
                if not slug:
                    continue
                skill.append(-float(pred.get("predicted_rank", 0)))
                realized.append(pm.get(slug, 0.0))

            note = ""
            rho = spearman(skill, realized)
            if rho is None:
                if len(skill) < 3:
                    note = "too few predicted"
                elif len(set(realized)) < 2:
                    note = "no realized spread"
                else:
                    note = "degenerate"
            else:
                per_stage_rho.append(rho)
                rr = _rankdata([-v for v in realized])  # higher pts -> lower rank #
                for pr, r_rank in zip(skill, rr):
                    pooled_pred_rank.append(-pr)     # back to predicted_rank
                    pooled_realized_rank.append(r_rank)

            # top-10 hit rate
            pred_top = [
                p.get("rider_slug")
                for p in preds
                if p.get("predicted_rank", 1e9) <= TOP_HIT and p.get("rider_slug")
            ]
            real_top = _realized_top(target, TOP_HIT)
            if real_top and pred_top:
                hits = len(set(pred_top) & set(real_top))
                frac = hits / float(len(real_top))
                top_hit_fracs.append(frac)
                top_str = f"{hits}/{len(real_top)}"
            else:
                top_str = "-"

            rho_str = f"{rho:+.3f}" if rho is not None else "   n/a "
            print(
                f"{race:>8}  {str(stage_no):>3}  "
                f"{str(target.get('profile_type')):>8}  "
                f"{str(target.get('finish_type')):>7}  "
                f"{len(preds):>4}  {rho_str:>7}  {top_str:>5}  {note}"
            )

    # -- aggregates ----------------------------------------------------------
    print("-" * 78)
    macro = (sum(per_stage_rho) / len(per_stage_rho)) if per_stage_rho else None
    pooled = spearman(
        [-v for v in pooled_pred_rank],       # skill = -predicted_rank
        [-v for v in pooled_realized_rank],   # skill = -realized_rank (1=best)
    )
    top_hit = (sum(top_hit_fracs) / len(top_hit_fracs)) if top_hit_fracs else None

    print("\nSummary")
    print(f"  stages evaluated          : {evaluated}")
    print(f"  stages with valid rho     : {len(per_stage_rho)}")
    if macro is not None:
        print(f"  macro-avg Spearman rho    : {macro:+.4f}   (mean of per-stage rho)")
    else:
        print("  macro-avg Spearman rho    : n/a")
    if pooled is not None:
        print(f"  pooled Spearman rho       : {pooled:+.4f}   (within-stage realized ranks)")
    else:
        print("  pooled Spearman rho       : n/a")
    if top_hit is not None:
        print(f"  mean top-{TOP_HIT} hit-rate      : {top_hit:.3f}   "
              f"(predicted top-{TOP_HIT} landing in realized top-{TOP_HIT})")
    else:
        print(f"  mean top-{TOP_HIT} hit-rate      : n/a")

    # interpretation hint (mirror validate_predictor thresholds)
    verdict = None
    if macro is not None:
        if macro >= 0.30:
            verdict = "predictor carries real per-stage signal"
        elif macro >= 0.10:
            verdict = "weak but positive per-stage signal"
        else:
            verdict = "little/no per-stage rank signal"
        print(f"  verdict                   : {verdict}")

    # persist a small JSON so the plan/report can cite it
    out = {
        "corpus_file": str(store.path.relative_to(ROOT)).replace("\\", "/"),
        "race_filter": race_filter,
        "total_stages": len(all_stages),
        "stages_evaluated": evaluated,
        "stages_with_rho": len(per_stage_rho),
        "stages_with_top10_prediction": len(top_hit_fracs),
        "macro_spearman": round(macro, 4) if macro is not None else None,
        "pooled_spearman": round(pooled, 4) if pooled is not None else None,
        "mean_top10_hit_rate": round(top_hit, 4) if top_hit is not None else None,
        "verdict": verdict,
        "protocol_version": PROTOCOL_VERSION,
        "evaluation_mode": EVALUATION_MODE,
        "protocol": (
            "pre-race cross-race holdout; all completed prior Grand Tours; "
            "exact rider_slug matching"
        ),
    }
    if race_filter:
        print("\nFiltered diagnostic run; canonical validation artifact was not changed.")
        return 0
    out_path = ROOT / "data" / "pcs" / "pcs_validation.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
