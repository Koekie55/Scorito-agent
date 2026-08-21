"""Rider-per-stage expected-points model.

Two layers:

1. A deterministic **heuristic** that weights each rider's quality ratings by
   how relevant that quality is to a stage's profile (flat sprint, mountain,
   ITT, ...). Needs no training data — always available.

2. An optional **calibrated linear model** (:class:`StageScorer`) that fits the
   relevance-weighted quality features to the *real* Scorito points from a
   snapshot, so predictions come out on the same scale as actual points.

The quality<->profile relevance encodes the domain knowledge decoded from the
TdF 2026 market (see ``models`` for the enum meanings).
"""

from __future__ import annotations

from .models import Rider, Snapshot, Stage

# quality types (see models.QUALITY_LABELS)
Q_GC, Q_CLIMB, Q_TT, Q_SPRINT, Q_PUNCH, Q_HILLY, Q_COBBLES = range(7)

_ALL_Q = (Q_GC, Q_CLIMB, Q_TT, Q_SPRINT, Q_PUNCH, Q_HILLY, Q_COBBLES)


def quality_relevance(stage: Stage) -> dict[int, float]:
    """Map quality type -> relevance weight (0..1) for this stage profile."""
    if stage.is_itt:
        return {Q_TT: 1.0, Q_GC: 0.4, Q_CLIMB: 0.15}
    if stage.is_ttt:
        # Team time trial: TT ability dominates; whole team shares the result.
        return {Q_TT: 1.0, Q_GC: 0.3}
    # Road stage — split by terrain.
    if stage.terrain_type == 1:  # Flat / bunch sprint
        return {Q_SPRINT: 1.0, Q_COBBLES: 0.5, Q_PUNCH: 0.2}
    if stage.terrain_type == 2:  # Hilly / rolling
        return {Q_PUNCH: 1.0, Q_HILLY: 0.9, Q_GC: 0.35, Q_CLIMB: 0.3, Q_SPRINT: 0.15}
    if stage.terrain_type == 3:  # Mountain / summit finish
        return {Q_CLIMB: 1.0, Q_GC: 1.0, Q_HILLY: 0.35}
    # Unknown terrain — mild all-round weighting.
    return {q: 0.3 for q in _ALL_Q}


def heuristic_score(rider: Rider, stage: Stage) -> float:
    """Un-calibrated relevance-weighted quality sum (higher = better fit)."""
    rel = quality_relevance(stage)
    return sum(w * rider.quality(q) for q, w in rel.items())


def _features(rider: Rider, stage: Stage) -> list[float]:
    """Relevance-weighted quality vector (one feature per quality type)."""
    rel = quality_relevance(stage)
    return [rel.get(q, 0.0) * rider.quality(q) for q in _ALL_Q]


class StageScorer:
    """Linear model: relevance-weighted qualities -> expected Scorito points.

    Falls back to the heuristic (scaled) when scikit-learn is unavailable or
    the model has not been fitted.
    """

    def __init__(self) -> None:
        self._model = None
        self._fitted = False

    def fit(self, snapshot: Snapshot) -> "StageScorer":
        """Train on every (rider, stage) pair with its real summed points."""
        X: list[list[float]] = []
        y: list[float] = []
        for stage in snapshot.stages:
            for rider in snapshot.riders:
                X.append(_features(rider, stage))
                y.append(snapshot.actual_points(rider.rider_id, stage))
        try:
            from sklearn.linear_model import Ridge

            model = Ridge(alpha=1.0, positive=True)
            model.fit(X, y)
            self._model = model
            self._fitted = True
        except Exception:  # pragma: no cover - sklearn missing / degenerate data
            self._model = None
            self._fitted = False
        return self

    def expected(self, rider: Rider, stage: Stage) -> float:
        """Expected points for a rider on a stage."""
        if self._fitted and self._model is not None:
            pred = float(self._model.predict([_features(rider, stage)])[0])
            return max(0.0, pred)
        return heuristic_score(rider, stage)

    def season_value(self, rider: Rider, snapshot: Snapshot) -> float:
        """Sum of expected points over every stage (forward-looking value)."""
        return sum(self.expected(rider, s) for s in snapshot.stages)
