import json

from scorito_agent.tv2_axelgaard import (
    archive_preview,
    discover_previews,
    is_usable_before_stage,
    parse_preview,
)

STAR = "\u2b50"

PREVIEW_HTML = """
<html><head>
  <script type="application/ld+json">{
    "@type": "NewsArticle",
    "headline": "Axelgaards optakt til 3. etape af Vuelta a Espana",
    "description": "Udsigt til en puncheurspurt eller en udbrudssejr.",
    "url": "https://sport.tv2.dk/cykling/example",
    "datePublished": "2026-06-03T09:30:31.033Z",
    "dateModified": "2026-08-23T21:47:28.462Z"
  }</script>
</head><body><div class="tc_richcontent">
  <ul><li>174 km</li><li>Mandag 24. august 2026</li><li>Start/i m&#229;l: 13.21 / ca. 17.30</li></ul>
  <h2>Favoritter</h2>
  <p>{s}{s}{s} Tadej Pogacar</p>
  <p>{s} Oscar Onley, Thibau Nys</p>
  <p>Kandidater til et udbrud: Jay Vine, Harold Tejada</p>
  <p>BEM&#198;RK: Jeg er i tvivl om, hvorvidt UAE lader udbruddet g&#229;.</p>
  <h2>Kort analyse af etapen</h2>
  <p>Jeg regner med en spurt p&#229; toppen, men et udbrud kan holde.</p>
  <h2>Favoritterne</h2>
  <p>Selvf&#248;lgelig er Tadej Pogacar favorit, og Wout van Aert kan ogs&#229; noget.</p>
</div></body></html>
""".replace("{s}", STAR)


def test_parse_preview_extracts_tiers_breakaway_notes_and_schedule() -> None:
    preview = parse_preview(PREVIEW_HTML, source_url="https://wrong.example")

    assert preview["stage_number"] == 3
    assert preview["source_url"] == "https://sport.tv2.dk/cykling/example"
    assert preview["published_at"] == "2026-06-03T09:30:31.033000+00:00"
    assert preview["modified_at"] == "2026-08-23T21:47:28.462000+00:00"
    assert preview["stage_date"] == "2026-08-24"
    assert preview["start_time_local"] == "13:21"
    assert preview["rider_tiers"] == [
        {"rider": "Tadej Pogacar", "stars": 3},
        {"rider": "Oscar Onley", "stars": 1},
        {"rider": "Thibau Nys", "stars": 1},
    ]
    assert preview["breakaway_candidates"] == ["Jay Vine", "Harold Tejada"]
    assert [note["kind"] for note in preview["notes"]] == ["BEM\u00c6RK"]
    assert len(preview["content_sha256"]) == 64


def test_prose_mentions_never_become_star_tiers() -> None:
    preview = parse_preview(PREVIEW_HTML, source_url="https://example")

    tiered = {row["rider"] for row in preview["rider_tiers"]}
    assert "Wout van Aert" not in tiered


def test_scenario_signal_uses_stage_analysis_not_the_whole_page() -> None:
    preview = parse_preview(PREVIEW_HTML, source_url="https://example")

    assert preview["scenario_probabilities_raw"] == {
        "reduced_sprint": 0.5,
        "breakaway": 0.5,
    }
    assert "Favoritterne" not in preview["scenario_text_da"]


def test_discover_previews_reads_stage_and_race_from_links() -> None:
    html = """
    <a href="https://sport.tv2.dk/cykling/2026-06-03-axelgaards-optakt-til-2-etape-af-vuelta-a-espana">a</a>
    <a href="https://sport.tv2.dk/cykling/2026-06-03-axelgaards-optakt-til-2-etape-af-vuelta-a-espana">dup</a>
    <a href="https://sport.tv2.dk/cykling/2026-08-22-axelgaards-optakt-til-5-etape-af-renewi-tour">b</a>
    <a href="https://sport.tv2.dk/cykling/2026-08-23-kometen-udstillede-mads-ps-problem">not a preview</a>
    """

    assert discover_previews(html) == [
        {
            "url": "https://sport.tv2.dk/cykling/2026-08-22-axelgaards-optakt-til-5-etape-af-renewi-tour",
            "url_date": "2026-08-22",
            "stage_number": 5,
            "race_slug": "renewi-tour",
        },
        {
            "url": "https://sport.tv2.dk/cykling/2026-06-03-axelgaards-optakt-til-2-etape-af-vuelta-a-espana",
            "url_date": "2026-06-03",
            "stage_number": 2,
            "race_slug": "vuelta-a-espana",
        },
    ]


def test_archive_keeps_one_revision_per_content_hash(tmp_path) -> None:
    first = parse_preview(PREVIEW_HTML, source_url="https://example")

    initial = archive_preview(tmp_path, first, race_slug="vuelta2026", fetched_at="2026-08-23T22:00:00+00:00")
    repeat = archive_preview(tmp_path, first, race_slug="vuelta2026", fetched_at="2026-08-23T23:00:00+00:00")

    assert initial["revision_created"] is True
    assert initial["content_changed"] is True
    assert repeat["revision_created"] is False
    assert repeat["content_changed"] is False

    edited = parse_preview(PREVIEW_HTML.replace("Jay Vine", "Jay Vine, Sepp Kuss"), source_url="https://example")
    updated = archive_preview(tmp_path, edited, race_slug="vuelta2026", fetched_at="2026-08-24T05:00:00+00:00")

    assert updated["revision_created"] is True
    assert updated["content_changed"] is True
    assert len(list((tmp_path / "vuelta2026" / "revisions").glob("*.json"))) == 2
    latest = json.loads((tmp_path / "vuelta2026" / "stage-03.json").read_text(encoding="utf-8"))
    assert "Sepp Kuss" in latest["breakaway_candidates"]


def test_preview_edited_after_the_start_is_not_usable() -> None:
    before = {
        "stage_date": "2026-08-24",
        "start_time_local": "13:21",
        "modified_at": "2026-08-23T21:47:28+00:00",
    }
    after = {**before, "modified_at": "2026-08-24T13:30:00+00:00"}

    assert is_usable_before_stage(before) is True
    assert is_usable_before_stage(after) is False
    assert is_usable_before_stage({"stage_date": None, "modified_at": None}) is False


def _model_projection() -> dict:
    from tests.test_refresh_vuelta_stage_predictions import _projection

    return _projection()


def _build(monkeypatch, stars: dict, weight: float) -> dict:
    from scripts import refresh_vuelta_stage_predictions as module

    monkeypatch.setattr(module, "stage_star_signals", lambda _dir: stars)
    monkeypatch.setattr(module, "validated_weight", lambda _path: (weight, "test"))
    monkeypatch.setattr(module, "load_forum_opinion", lambda _path: {})
    monkeypatch.setattr(module, "_expert_chat_by_key", dict)
    return module.build_stage_top20(_model_projection(), {"stage_breakdown": {}}, {})


def test_unvalidated_preview_cannot_move_the_model(monkeypatch) -> None:
    from scripts.refresh_vuelta_stage_predictions import _name_key

    stars = {1: {_name_key("Rider 21"): 5.0}}

    without = _build(monkeypatch, {}, 0.0)
    with_zero_weight = _build(monkeypatch, stars, 0.0)

    baseline = [row["rider_slug"] for row in without["stages"][0]["top_20"]]
    gated = [row["rider_slug"] for row in with_zero_weight["stages"][0]["top_20"]]
    assert gated == baseline
    assert all(
        row["tv2_axelgaard_multiplier"] == 1.0
        for row in with_zero_weight["stages"][0]["top_20"]
    )


def test_validated_stars_raise_the_objective_score_by_the_earned_weight(monkeypatch) -> None:
    from scripts.refresh_vuelta_stage_predictions import _name_key

    stars = {1: {_name_key("Rider 3"): 5.0}}

    without = _build(monkeypatch, {}, 0.0)
    with_signal = _build(monkeypatch, stars, 0.10)

    before = next(r for r in without["stages"][0]["top_20"] if r["rider_slug"] == "rider-3")
    after = next(r for r in with_signal["stages"][0]["top_20"] if r["rider_slug"] == "rider-3")

    assert after["tv2_axelgaard_stars"] == 5.0
    assert after["tv2_axelgaard_multiplier"] == 1.10
    assert after["objective_score"] > before["objective_score"]
    assert with_signal["stages"][0]["tv2_axelgaard_ranked_riders"] == 1
    assert with_signal["stages"][1]["tv2_axelgaard_ranked_riders"] == 0


def test_stage_without_credited_points_is_excluded_from_validation() -> None:
    from scripts.validate_tv2_axelgaard import scored_stages

    class Rider:
        def __init__(self, rider_id):
            self.rider_id = rider_id
            self.name = f"Rider {rider_id}"

    class Snapshot:
        riders = [Rider(1), Rider(2)]

        def stage_by_order(self, stage_no):
            return None if stage_no == 9 else f"stage-{stage_no}"

        def actual_points(self, rider_id, stage):
            # stage 3 has finished but Scorito has not credited its points yet
            return 0.0 if stage == "stage-3" else 12.0

    assert scored_stages(Snapshot(), [1, 2, 3, 9]) == [1, 2]
