"""Expected Scorito teammate points for stage enrollment decisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import Snapshot

STAGE_WIN_TEAM_POINTS = 10.0
CLASSIFICATION_TEAM_POINT_TYPES = frozenset({201, 202, 203, 204})

# Calibrated on all 40 next-stage transitions in the completed 2026 Tour and
# Giro snapshots. Values are retained-team frequencies by Scorito PointsType.
DEFAULT_CLASSIFICATION_RETENTION = {
    201: 33 / 40,
    202: 34 / 40,
    203: 34 / 40,
    204: 31 / 40,
}


@dataclass(frozen=True)
class TeamPointProjection:
    """Expected teammate points, kept separate by scoring route."""

    classification_points: float = 0.0
    stage_win_points: float = 0.0

    @property
    def total(self) -> float:
        return self.classification_points + self.stage_win_points


def latest_classification_team_state(
    snapshot: Snapshot,
    *,
    before_stage_order: int,
) -> tuple[int | None, dict[int, dict[int, float]]]:
    """Return the latest prior round's classification points by trade team."""
    prior_stages = [
        stage
        for stage in snapshot.stages
        if stage.order < before_stage_order
        and any(
            key[0] == stage.market_round_id
            for key in snapshot.stage_point_components
        )
    ]
    if not prior_stages:
        return None, {}

    latest = max(prior_stages, key=lambda stage: stage.order)
    state: dict[int, dict[int, float]] = {}
    for rider in snapshot.riders:
        components = snapshot.actual_point_components(rider.rider_id, latest)
        team_state = state.setdefault(rider.team_id, {})
        for points_type, value in components.items():
            if points_type in CLASSIFICATION_TEAM_POINT_TYPES and value > 0:
                team_state[points_type] = max(team_state.get(points_type, 0.0), value)
    return latest.order, {team_id: values for team_id, values in state.items() if values}


def normalized_team_win_probabilities(
    snapshot: Snapshot,
    rider_win_scores: Mapping[int, float],
) -> dict[int, float]:
    """Normalize non-negative rider win scores and aggregate them by team."""
    positive_scores = {
        rider_id: max(0.0, float(score))
        for rider_id, score in rider_win_scores.items()
        if snapshot.rider(rider_id) is not None
    }
    denominator = sum(positive_scores.values())
    if denominator <= 0:
        return {}

    probabilities: dict[int, float] = {}
    for rider_id, score in positive_scores.items():
        team_id = snapshot.rider(rider_id).team_id
        probabilities[team_id] = probabilities.get(team_id, 0.0) + score / denominator
    return probabilities


def expected_team_points_by_rider(
    snapshot: Snapshot,
    *,
    stage_order: int,
    team_win_probabilities: Mapping[int, float],
    retention_probabilities: Mapping[int, float] = DEFAULT_CLASSIFICATION_RETENTION,
) -> dict[int, TeamPointProjection]:
    """Project next-stage teammate points from prior state and win chances."""
    invalid = {
        team_id: probability
        for team_id, probability in team_win_probabilities.items()
        if not 0.0 <= float(probability) <= 1.0
    }
    if invalid:
        raise ValueError(f"team win probabilities outside [0, 1]: {invalid}")
    if sum(float(value) for value in team_win_probabilities.values()) > 1.000001:
        raise ValueError("team win probabilities sum above 1")

    source_stage, state = latest_classification_team_state(
        snapshot,
        before_stage_order=stage_order,
    )
    horizon = stage_order - source_stage if source_stage is not None else 0
    output: dict[int, TeamPointProjection] = {}
    for rider in snapshot.riders:
        classification = sum(
            value * float(retention_probabilities.get(points_type, 0.0)) ** horizon
            for points_type, value in state.get(rider.team_id, {}).items()
        )
        stage_win = STAGE_WIN_TEAM_POINTS * float(
            team_win_probabilities.get(rider.team_id, 0.0)
        )
        output[rider.rider_id] = TeamPointProjection(classification, stage_win)
    return output


def estimate_classification_retention(
    snapshots: Sequence[Snapshot],
) -> dict[int, tuple[int, int, float]]:
    """Estimate retained-team rates from consecutive completed stages."""
    counts = {points_type: [0, 0] for points_type in CLASSIFICATION_TEAM_POINT_TYPES}
    for snapshot in snapshots:
        states = [
            latest_classification_team_state(
                snapshot,
                before_stage_order=stage.order + 1,
            )[1]
            for stage in snapshot.stages
            if any(key[0] == stage.market_round_id for key in snapshot.stage_point_components)
        ]
        for prior, current in zip(states, states[1:]):
            prior_holders = {
                points_type: team_id
                for team_id, values in prior.items()
                for points_type in values
            }
            current_holders = {
                points_type: team_id
                for team_id, values in current.items()
                for points_type in values
            }
            for points_type, team_id in prior_holders.items():
                counts[points_type][1] += 1
                counts[points_type][0] += current_holders.get(points_type) == team_id
    return {
        points_type: (retained, transitions, retained / transitions)
        for points_type, (retained, transitions) in sorted(counts.items())
        if transitions
    }
