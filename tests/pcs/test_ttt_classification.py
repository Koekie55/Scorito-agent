"""Team time trials must not be classified as individual time trials.

Regression guard: PCS writes "Team time trial" in the "Won how" detail and marks
the stage title as "S3 (TTT) Stage 3 - ...". A substring test for "time trial"
matches both TTT and ITT, and a test for " ttt" never matches "(TTT)", so team
results used to be credited as individual time-trial evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import pytest  # noqa: E402

from scorito_agent.pcs.parse import is_team_time_trial, parse_stage_page  # noqa: E402
from scripts.project_vuelta import _profile_transfer, _result_profile  # noqa: E402

TTT_PAGE = """
<html><body>
<div class="page-title"><h1>S3 (TTT) Stage 3 - Cosne-Cours-sur-Loire &gt; Pouilly-sur-Loire</h1></div>
<ul class="infolist">
  <li><div class="title">Date:</div><div class="value">10 March 2026</div></li>
  <li><div class="title">Distance:</div><div class="value">23.5 km</div></li>
  <li><div class="title">Won how:</div><div class="value">Team time trial</div></li>
  <li><div class="title">Vertical meters:</div><div class="value">212</div></li>
</ul>
<table class="results"><thead><tr><th>Rnk</th><th>Rider</th></tr></thead>
<tbody><tr><td>1</td><td><a href="rider/oscar-onley">Oscar Onley</a></td></tr></tbody></table>
</body></html>
"""

ITT_PAGE = TTT_PAGE.replace(
    "S3 (TTT) Stage 3 - Cosne-Cours-sur-Loire &gt; Pouilly-sur-Loire",
    "S1 (ITT) Stage 1 (ITT) - Monaco &gt; Monaco",
).replace("Team time trial", "Time trial")

NORMAL_STAGE_WITH_TTT_NAVIGATION = """
<html><body>
<div class="page-title"><h1>Stage 4 - Vichy &gt; La Loge des Gardes</h1></div>
<div class="profile-type">Hilly</div>
<select class="stage-navigation"><option>Stage 3 (TTT) - Perreux &gt; Perreux</option></select>
<ul class="infolist">
    <li><div class="title">Won how:</div><div class="value">Sprint of a small group</div></li>
</ul>
</body></html>
"""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Team time trial", True),
        ("S3 (TTT) Stage 3 - Perreux > Perreux", True),
        ("ttt", True),
        ("Time trial", False),
        ("Individual time trial", False),
        ("S1 (ITT) Stage 1 - Monaco > Monaco", False),
        ("Sprint of a small group", False),
    ],
)
def test_is_team_time_trial(text: str, expected: bool) -> None:
    assert is_team_time_trial(text) is expected


def test_ttt_stage_page_is_not_itt() -> None:
    stage = parse_stage_page(TTT_PAGE)
    assert stage["profile_type"] == "ttt"
    assert stage["finish_type"] == "tt"


def test_itt_stage_page_still_itt() -> None:
    stage = parse_stage_page(ITT_PAGE)
    assert stage["profile_type"] == "itt"


def test_ttt_in_stage_navigation_does_not_classify_current_stage_as_ttt() -> None:
    stage = parse_stage_page(NORMAL_STAGE_WITH_TTT_NAVIGATION)
    assert stage["profile_type"] == "hilly"


def test_result_profile_rejects_stale_itt_context_for_ttt() -> None:
    """A cache built before the fix says 'itt'; the title guard must override."""
    stale = {
        "race": "S3 (TTT) Stage 3 - Cosne-Cours-sur-Loire > Pouilly-sur-Loire",
        "event": "Paris - Nice (2.UWT)",
        "result_url": "race/paris-nice/2026/stage-3",
        "course_context": {"profile_type": "itt", "finish_type": "tt"},
    }
    profile, finish, basis = _result_profile(stale, {})
    assert profile == "ttt"
    assert finish == "tt"
    assert "TTT" in basis


def test_ttt_transfers_weakly_to_itt() -> None:
    assert _profile_transfer("itt", "itt") == 1.0
    assert _profile_transfer("ttt", "itt") == 0.08
