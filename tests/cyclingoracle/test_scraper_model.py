from __future__ import annotations

from pathlib import Path

from scorito_agent.cyclingoracle.model import rank_riders
from scorito_agent.cyclingoracle.scraper import (
    extract_stage_metadata,
    parse_data_lists,
    parse_rider_stats,
    stage_predictions,
)

FIXTURES = Path(__file__).parent / "fixtures"
STAGE_URL = "https://www.cyclingoracle.com/nl/blog/tour-de-france-2026-voorspelling-etappe-21"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def test_stage_prediction_parser_extracts_expected_rows() -> None:
    rows = stage_predictions(STAGE_URL, html=_fixture("blog_tdf_stage21.html"))

    assert len(rows) == 15
    assert rows[0]["rider_name"] == "Mathieu van der Poel"
    assert rows[0]["rider_id"] == "16672"
    assert rows[0]["predicted_rank"] == 1
    assert rows[0]["win_probability_pct"] == 26.78
    assert rows[0]["stage_number"] == 21
    assert rows[0]["stage_profile"] == "hilly_sprint"


def test_data_lists_and_rider_page_expose_model_features() -> None:
    data_rows = parse_data_lists(_fixture("blog_tdf_datalijsten.html"))
    tadej_ovr = [
        row
        for row in data_rows
        if row["metric"] == "ovr" and row["rider_id"] == "45992"
    ]
    mathieu = parse_rider_stats(_fixture("rider_mathieu-van-der-poel-16672.html"))

    assert len(data_rows) > 100
    assert tadej_ovr[0]["value"] == 99
    assert mathieu["rider_name"] == "Mathieu van der Poel"
    assert mathieu["team_name"] == "Alpecin-Premier Tech"
    assert mathieu["stats"]["hll"] == 92
    assert mathieu["stats"]["leadout"] == 93
    assert mathieu["stats"]["one_day"] == 96


def test_model_ranks_known_hilly_sprint_stage_sensibly() -> None:
    stage_html = _fixture("blog_tdf_stage21.html")
    stage = extract_stage_metadata(stage_html, STAGE_URL)
    riders = [
        parse_rider_stats(_fixture(name))
        for name in [
            "rider_mathieu-van-der-poel-16672.html",
            "rider_tadej-pogacar-45992.html",
            "rider_jasper-philipsen-45363.html",
            "rider_mads-pedersen-16793.html",
            "rider_tom-pidcock-65025.html",
            "rider_remco-evenepoel-84019.html",
        ]
    ]

    ranked = rank_riders(riders, stage=stage)

    assert stage["profile"] == "hilly_sprint"
    assert ranked[0]["rider_name"] == "Mathieu van der Poel"
    assert ranked[1]["rider_name"] == "Tadej Pogačar"
    assert ranked[0]["model_score"] > ranked[-1]["model_score"]
    assert abs(sum(row["model_probability"] for row in ranked) - 1.0) < 1e-9

