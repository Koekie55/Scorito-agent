"""Bridge external stage predictions into the Scorito points optimizer.

Parts 2 (cyclingoracle) and 3 (procyclingstats) both produce **rankings** of
who will finish where in a stage — they are *rankers*, not Scorito-points
producers. The optimizer, however, needs an ``expected``/``season_value``
scorer that talks in Scorito points. This module is the seam between the two:

1. :class:`RankPointsCurve` learns, from a real snapshot, the empirical
   *finishing-rank -> mean Scorito stage points* curve **per stage type**
   (road / ITT / TTT). The snapshot never stores finishing order, but the
   per-stage points are themselves a strict rank-based finishing table, so
   sorting riders by their actual points recovers the finishing ranks and the
   points paid at each rank.

2. :func:`predictions_from_pcs` / :func:`predictions_from_cyclingoracle`
   normalise the two external models' outputs into a common
   ``{stage_id -> {name_key -> predicted_rank}}`` shape.

3. :class:`ExternalStageScorer` is a drop-in replacement for
   :class:`~scorito_agent.scorito.scoring.StageScorer`: it converts a rider's
   *predicted* rank on a stage into Scorito points via the curve, so the whole
   forward pipeline (``expected_total_values`` -> ``pick_squad`` ->
   ``best_stage_lineup``) runs unchanged on external predictions.

Everything here is offline and dependency-free; it is verified with a synthetic
fixture and a "perfect oracle" self-back-test (feed the snapshot's own derived
ranks back through the curve and confirm the recovered squad lands near the
hindsight ceiling).
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol

from .models import Rider, Snapshot, Stage


def name_key(name: Any) -> str:
    """Normalise a rider name for cross-source matching.

    NFKD accent-strip -> lowercase -> alphanumerics only. Mirrors
    ``cyclingoracle.model._name_key`` so keys align across all three sources.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", str(name))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return "".join(ch for ch in text if ch.isalnum())


class _Scorer(Protocol):
    def expected(self, rider: Rider, stage: Stage) -> float: ...
    def season_value(self, rider: Rider, snapshot: Snapshot) -> float: ...


@dataclass
class RankPointsCurve:
    """Empirical finishing-rank -> mean Scorito points, per stage type."""

    # stage_type -> {rank -> mean points}
    curves: dict[int, dict[int, float]] = field(default_factory=dict)

    @classmethod
    def from_snapshot(cls, snapshot: Snapshot) -> "RankPointsCurve":
        """Derive the curve by sorting riders per stage by their real points.

        Per stage: riders with >0 points are ranked 1..N in descending points;
        that ``(stage_type, rank) -> points`` sample is accumulated, then the
        per-rank samples are averaged across all stages of the same type.
        """
        samples: dict[int, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
        for stage in snapshot.stages:
            scored = [
                (r.rider_id, snapshot.actual_points(r.rider_id, stage))
                for r in snapshot.riders
            ]
            scored = [(rid, pts) for rid, pts in scored if pts > 0]
            scored.sort(key=lambda t: t[1], reverse=True)
            for rank, (_rid, pts) in enumerate(scored, start=1):
                samples[stage.stage_type][rank].append(float(pts))

        curves: dict[int, dict[int, float]] = {}
        for stage_type, per_rank in samples.items():
            curve = {rank: sum(vals) / len(vals) for rank, vals in per_rank.items()}
            # Enforce monotic-decreasing means so a better predicted rank never
            # pays less than a worse one (guards against sparse-sample noise).
            best = float("inf")
            for rank in sorted(curve):
                best = min(best, curve[rank])
                curve[rank] = best
            curves[stage_type] = curve
        return cls(curves=curves)

    def _curve_for(self, stage_type: int) -> dict[int, float]:
        if stage_type in self.curves:
            return self.curves[stage_type]
        # Fall back to the road curve (type 1) or any available curve.
        if 1 in self.curves:
            return self.curves[1]
        if self.curves:
            return next(iter(self.curves.values()))
        return {}

    def points_for(self, stage_type: int, rank: int | float | None) -> float:
        """Scorito points paid for finishing ``rank`` on a ``stage_type`` stage.

        Ranks at or before the best known rank clamp to rank 1; ranks beyond the
        last points-paying position return 0.0.
        """
        if rank is None:
            return 0.0
        curve = self._curve_for(stage_type)
        if not curve:
            return 0.0
        r = int(round(float(rank)))
        if r < 1:
            r = 1
        if r in curve:
            return curve[r]
        max_rank = max(curve)
        if r > max_rank:
            return 0.0
        # Sparse curve: nearest known rank <= r (curve is monotone).
        known = [k for k in curve if k <= r]
        return curve[max(known)] if known else 0.0


PredictionsByStage = dict[int, dict[str, int]]


class ExternalStageScorer:
    """Score riders from external per-stage rank predictions.

    ``predictions_by_stage`` maps a Scorito ``stage_id`` to
    ``{name_key -> predicted_rank}``. A matched rider's expected points are the
    curve value for their predicted rank; unmatched riders defer to ``fallback``
    (which must be points-scaled, e.g. a fitted ``StageScorer``) or score 0.

    Exposes exactly ``expected``/``season_value`` so it is a drop-in for
    :class:`StageScorer` in :func:`optimizer.expected_total_values`.
    """

    def __init__(
        self,
        predictions_by_stage: PredictionsByStage,
        curve: RankPointsCurve,
        fallback: _Scorer | None = None,
    ) -> None:
        self.predictions_by_stage = predictions_by_stage
        self.curve = curve
        self.fallback = fallback

    def expected(self, rider: Rider, stage: Stage) -> float:
        ranks = self.predictions_by_stage.get(stage.stage_id, {})
        key = name_key(rider.name)
        if key in ranks:
            return self.curve.points_for(stage.stage_type, ranks[key])
        if self.fallback is not None:
            return self.fallback.expected(rider, stage)
        return 0.0

    def season_value(self, rider: Rider, snapshot: Snapshot) -> float:
        return sum(self.expected(rider, s) for s in snapshot.stages)


def _row_name(row: Mapping[str, Any]) -> str:
    for key in ("rider", "rider_name", "name", "rider_slug", "slug"):
        value = row.get(key)
        if value:
            return str(value)
    return ""


def predictions_from_pcs(
    pcs_by_stage: Mapping[int, Mapping[str, Any]],
) -> PredictionsByStage:
    """Build ``{stage_id -> {name_key -> rank}}`` from PCS predict outputs.

    ``pcs_by_stage`` maps a Scorito ``stage_id`` to the dict returned by
    :func:`pcs.predict.predict_finishers` (i.e. having a ``"predictions"`` list
    of ``{rider, rider_slug, predicted_rank, ...}``).
    """
    out: PredictionsByStage = {}
    for stage_id, result in pcs_by_stage.items():
        ranks: dict[str, int] = {}
        predictions = result.get("predictions", []) if isinstance(result, Mapping) else result
        for index, pred in enumerate(predictions, start=1):
            key = name_key(_row_name(pred))
            if not key:
                continue
            rank = pred.get("predicted_rank", index) if isinstance(pred, Mapping) else index
            ranks.setdefault(key, int(rank))
        out[int(stage_id)] = ranks
    return out


def predictions_from_cyclingoracle(
    ranked_by_stage: Mapping[int, Iterable[Mapping[str, Any]]],
) -> PredictionsByStage:
    """Build ``{stage_id -> {name_key -> rank}}`` from cyclingoracle rankings.

    ``ranked_by_stage`` maps a Scorito ``stage_id`` to the list of rows returned
    by :func:`cyclingoracle.model.rank_riders` (each row carries ``model_rank``
    and the rider's original name field).
    """
    out: PredictionsByStage = {}
    for stage_id, rows in ranked_by_stage.items():
        ranks: dict[str, int] = {}
        for index, row in enumerate(rows, start=1):
            key = name_key(_row_name(row))
            if not key:
                continue
            rank = row.get("model_rank", index) if isinstance(row, Mapping) else index
            ranks.setdefault(key, int(rank))
        out[int(stage_id)] = ranks
    return out


def perfect_oracle_predictions(snapshot: Snapshot) -> PredictionsByStage:
    """Derive each stage's true finishing ranks from the snapshot's own points.

    Used to self-test the rank->points->optimizer plumbing offline: feeding
    these back through the curve reconstructs each rider's season value and the
    recovered squad should land near the hindsight ceiling.
    """
    out: PredictionsByStage = {}
    for stage in snapshot.stages:
        scored = [
            (r, snapshot.actual_points(r.rider_id, stage)) for r in snapshot.riders
        ]
        scored = [(r, pts) for r, pts in scored if pts > 0]
        scored.sort(key=lambda t: t[1], reverse=True)
        ranks: dict[str, int] = {}
        for rank, (rider, _pts) in enumerate(scored, start=1):
            ranks.setdefault(name_key(rider.name), rank)
        out[stage.stage_id] = ranks
    return out
