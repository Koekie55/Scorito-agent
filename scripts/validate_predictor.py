r"""Validate the harvested cyclingoracle predictor against realized Scorito points.

This is the Part-2 "does the model actually predict?" check. It takes the
harvested cyclingoracle TdF-2026 stage predictions (win-probability rankings,
one row per rider per stage) and measures how well the predicted per-stage
ranking agrees with the *realized* per-stage Scorito points in the tdf2026
snapshot.

Pipeline
--------
1. Load ``data/cyclingoracle/tdf2026_predictions.jsonl`` (produced by
   ``scripts/harvest_cyclingoracle.py``): rows carry ``stage_number``,
   ``rider_name`` and ``model_rank`` (1 = most likely stage winner).
2. Group rows by cyclingoracle ``stage_number`` and map stage N -> Scorito
   ``stage_id = 2798 + N`` (TdF-2026 stages are 2799..2819).
3. Feed the ``{stage_id -> [rows]}`` mapping through the production seam
   :func:`predictions_from_cyclingoracle` -> ``{stage_id -> {name_key -> rank}}``
   and report the seam's native (exact ``name_key``) match rate against the
   snapshot's riders.
4. Because cyclingoracle stores names first-last ("Tadej Pogačar") while
   Scorito may store them last-first, exact ``name_key`` matching under-counts.
   A robust sorted-token matcher (order-independent) + last-name fallback is
   used for the actual correlation, and its match rate is reported too.
5. For every stage, Spearman-correlate the cyclingoracle skill signal
   (``-model_rank``, so higher = predicted better) against the rider's realized
   Scorito points on that stage, over the matched-rider set. Report per-stage
   and two pooled aggregates:
     * macro-average (mean of per-stage rho) - the headline number, and
     * a pooled Spearman computed on within-stage realized *ranks* (scale-free).

Run:
    $env:PYTHONPATH="src"; $env:PYTHONUTF8="1"; \
        .\.venv\Scripts\python.exe scripts\validate_predictor.py
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

# Make ``src`` importable when run as a plain script.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scorito_agent.scorito.external import (  # noqa: E402
    name_key,
    predictions_from_cyclingoracle,
)
from scorito_agent.scorito.loader import load_snapshot  # noqa: E402
from scorito_agent.scorito.models import Rider, Snapshot, Stage  # noqa: E402

PRED_PATH = ROOT / "data" / "cyclingoracle" / "tdf2026_predictions.jsonl"
STAGE_ID_BASE = 2798  # stage N -> stage_id 2798 + N
SNAPSHOT_SLUG = "tdf2026"

try:
    from scipy.stats import spearmanr as _scipy_spearmanr
except Exception:  # pragma: no cover - scipy is expected to be present
    _scipy_spearmanr = None


# ---------------------------------------------------------------------------
# name normalisation / matching helpers
# ---------------------------------------------------------------------------

def _norm_tokens(name: str) -> list[str]:
    """Accent-strip, lowercase, split into alphanumeric tokens.

    Mirrors :func:`name_key`'s normalisation but keeps token boundaries so we
    can build order-independent keys.
    """
    n = unicodedata.normalize("NFKD", name or "")
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    return re.findall(r"[a-z0-9]+", n)


def _key_sorted(name: str) -> str:
    """Order-independent key: sorted tokens joined.

    "Tadej Pogačar" and "Pogačar Tadej" both -> "pogacartadej".
    """
    return "".join(sorted(_norm_tokens(name)))


def _key_lastname(name: str) -> str:
    toks = _norm_tokens(name)
    return toks[-1] if toks else ""


class RiderMatcher:
    """Resolve a predicted rider name to a snapshot :class:`Rider`.

    Uses three strategies in priority order: exact ``name_key``, order-free
    sorted-token key, then a (uniqueness-guarded) last-name fallback.
    """

    def __init__(self, riders: list[Rider]) -> None:
        self._exact: dict[str, Rider] = {}
        self._sorted: dict[str, Rider] = {}
        self._lastname: dict[str, list[Rider]] = defaultdict(list)
        for r in riders:
            self._exact.setdefault(name_key(r.name), r)
            self._sorted.setdefault(_key_sorted(r.name), r)
            self._lastname[_key_lastname(r.name)].append(r)

    def match(self, name: str) -> tuple[Rider | None, str]:
        r = self._exact.get(name_key(name))
        if r is not None:
            return r, "exact"
        r = self._sorted.get(_key_sorted(name))
        if r is not None:
            return r, "sorted"
        bucket = self._lastname.get(_key_lastname(name), [])
        if len(bucket) == 1:
            return bucket[0], "lastname"
        return None, "unmatched"


# ---------------------------------------------------------------------------
# Spearman
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
    """Spearman rho, tie-aware, degenerate-safe. Prefer scipy when available."""
    if len(x) < 3:
        return None
    if _scipy_spearmanr is not None:
        rho, _p = _scipy_spearmanr(x, y)
        if rho is None or (isinstance(rho, float) and math.isnan(rho)):
            return None
        return float(rho)
    return _pearson(_rankdata(x), _rankdata(y))


# ---------------------------------------------------------------------------
# load + group harvested predictions
# ---------------------------------------------------------------------------

def load_predictions(path: Path) -> dict[int, list[dict[str, Any]]]:
    """Return ``{stage_id -> [rows]}`` grouped from the harvested JSONL."""
    by_stage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            sn = row.get("stage_number")
            if sn is None:
                continue
            stage_id = STAGE_ID_BASE + int(sn)
            by_stage[stage_id].append(row)
    # Order each stage's rows by model_rank so the seam's positional fallback
    # (enumerate) degrades gracefully if a row is missing model_rank.
    for rows in by_stage.values():
        rows.sort(key=lambda r: r.get("model_rank", r.get("predicted_rank", 1e9)))
    return dict(by_stage)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    if not PRED_PATH.exists():
        print(f"[error] predictions file not found: {PRED_PATH}", file=sys.stderr)
        print("        run scripts/harvest_cyclingoracle.py first.", file=sys.stderr)
        return 2

    by_stage = load_predictions(PRED_PATH)
    total_rows = sum(len(v) for v in by_stage.values())
    print(
        f"Loaded {total_rows} predicted rows across {len(by_stage)} stages "
        f"from {PRED_PATH.name}"
    )

    snapshot: Snapshot = load_snapshot(SNAPSHOT_SLUG)
    stages_by_id: dict[int, Stage] = {s.stage_id: s for s in snapshot.stages}
    matcher = RiderMatcher(snapshot.riders)
    print(
        f"Snapshot {snapshot.slug}: {len(snapshot.riders)} riders, "
        f"{len(snapshot.stages)} stages, budget {snapshot.budget / 1e6:.2f}M"
    )

    # -- seam sanity: run through the production adapter, report its native rate
    seam = predictions_from_cyclingoracle(by_stage)
    seam_keys = 0
    seam_matched = 0
    snap_exact = {name_key(r.name) for r in snapshot.riders}
    for _sid, keymap in seam.items():
        for k in keymap:
            seam_keys += 1
            if k in snap_exact:
                seam_matched += 1
    seam_rate = (seam_matched / seam_keys * 100.0) if seam_keys else 0.0
    print(
        f"\nSeam (predictions_from_cyclingoracle) exact name_key match: "
        f"{seam_matched}/{seam_keys} = {seam_rate:.1f}%"
    )

    # -- robust match + per-stage Spearman -----------------------------------
    print("\nPer-stage correlation (cyclingoracle rank vs realized Scorito points)")
    print(f"{'stage':>5}  {'type':>4}  {'pred':>4}  {'match':>5}  {'rho':>7}  note")
    print("-" * 60)

    per_stage_rho: list[float] = []
    match_strategy_counts: dict[str, int] = defaultdict(int)
    total_pred = 0
    total_matched = 0
    total_scoring_matched = 0  # matched riders that actually scored >0
    pooled_pred_rank: list[float] = []
    pooled_realized_rank: list[float] = []

    for stage_id in sorted(by_stage):
        rows = by_stage[stage_id]
        stage = stages_by_id.get(stage_id)
        stage_no = stage_id - STAGE_ID_BASE
        if stage is None:
            print(f"{stage_no:>5}  {'?':>4}  {len(rows):>4}  {'-':>5}  {'-':>7}  no snapshot stage")
            continue

        skill: list[float] = []       # -model_rank  (higher = predicted better)
        realized: list[float] = []    # realized Scorito points on this stage
        seen_ids: set[int] = set()
        matched = 0
        for row in rows:
            name = row.get("rider_name") or ""
            rider, how = matcher.match(name)
            match_strategy_counts[how] += 1
            total_pred += 1
            if rider is None or rider.rider_id in seen_ids:
                continue
            seen_ids.add(rider.rider_id)
            matched += 1
            total_matched += 1
            mrank = row.get("model_rank", row.get("predicted_rank"))
            if mrank is None:
                continue
            pts = snapshot.actual_points(rider.rider_id, stage)
            skill.append(-float(mrank))
            realized.append(float(pts))
            if pts > 0:
                total_scoring_matched += 1

        note = ""
        rho = spearman(skill, realized)
        if rho is None:
            if len(skill) < 3:
                note = "too few matched"
            elif len(set(realized)) < 2:
                note = "no realized spread (0 pts?)"
            else:
                note = "degenerate"
        else:
            per_stage_rho.append(rho)
            # pooled: convert realized points -> within-stage rank (1=best)
            rr = _rankdata([-v for v in realized])  # higher points -> lower rank number
            for pr, r_rank in zip(skill, rr):
                pooled_pred_rank.append(-pr)      # back to model_rank
                pooled_realized_rank.append(r_rank)

        stype = getattr(stage, "stage_type", "?")
        rho_str = f"{rho:+.3f}" if rho is not None else "   n/a "
        print(f"{stage_no:>5}  {str(stype):>4}  {len(rows):>4}  {matched:>5}  {rho_str:>7}  {note}")

    # -- aggregates ----------------------------------------------------------
    print("-" * 60)
    match_rate = (total_matched / total_pred * 100.0) if total_pred else 0.0
    macro = (sum(per_stage_rho) / len(per_stage_rho)) if per_stage_rho else None
    pooled = spearman(
        [-v for v in pooled_pred_rank],  # skill = -model_rank
        [-v for v in pooled_realized_rank],  # skill = -realized_rank (1=best -> highest)
    )

    print("\nSummary")
    print(f"  robust match rate         : {total_matched}/{total_pred} = {match_rate:.1f}%")
    print(f"    by strategy             : " + ", ".join(
        f"{k}={match_strategy_counts[k]}" for k in ("exact", "sorted", "lastname", "unmatched")
    ))
    print(f"  matched riders that scored: {total_scoring_matched} (>0 pts)")
    print(f"  stages with valid rho     : {len(per_stage_rho)}/{len(by_stage)}")
    if macro is not None:
        print(f"  macro-avg Spearman rho    : {macro:+.4f}   (mean of per-stage rho)")
    else:
        print("  macro-avg Spearman rho    : n/a")
    if pooled is not None:
        print(f"  pooled Spearman rho       : {pooled:+.4f}   (within-stage realized ranks)")
    else:
        print("  pooled Spearman rho       : n/a")

    # interpretation hint
    if macro is not None:
        if macro >= 0.30:
            verdict = "predictor carries real per-stage signal"
        elif macro >= 0.10:
            verdict = "weak but positive per-stage signal"
        else:
            verdict = "little/no per-stage rank signal (win-prob is GC-favourite biased)"
        print(f"  verdict                   : {verdict}")

    # persist a small JSON so the plan/report can cite it
    out = {
        "snapshot": snapshot.slug,
        "predictions_file": str(PRED_PATH.relative_to(ROOT)).replace("\\", "/"),
        "predicted_rows": total_rows,
        "stages": len(by_stage),
        "seam_exact_match_pct": round(seam_rate, 2),
        "robust_match_pct": round(match_rate, 2),
        "match_strategy_counts": dict(match_strategy_counts),
        "matched_scoring_riders": total_scoring_matched,
        "stages_with_rho": len(per_stage_rho),
        "macro_spearman": round(macro, 4) if macro is not None else None,
        "pooled_spearman": round(pooled, 4) if pooled is not None else None,
    }
    out_path = ROOT / "data" / "cyclingoracle" / "tdf2026_validation.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
