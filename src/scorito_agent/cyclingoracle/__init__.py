"""Part 2 — CyclingOracle scraper and prediction-model replication."""

from scorito_agent.cyclingoracle.model import rank_riders, score_rider
from scorito_agent.cyclingoracle.scraper import (
    list_races,
    list_stages,
    parse_data_lists,
    parse_rider_stats,
    stage_predictions,
)

__all__ = [
    "list_races",
    "list_stages",
    "parse_data_lists",
    "parse_rider_stats",
    "rank_riders",
    "score_rider",
    "stage_predictions",
]
