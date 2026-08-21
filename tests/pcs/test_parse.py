from pathlib import Path

from scorito_agent.pcs.parse import parse_stage_page, parse_startlist

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_stage_page_extracts_metadata_results_and_startlist() -> None:
    html = (FIXTURES / "stage_result.html").read_text(encoding="utf-8")

    stage = parse_stage_page(html, source_url="https://www.procyclingstats.com/race/tour-de-test/2026/stage-5")

    assert stage["race"] == "Tour de Test"
    assert stage["stage_no"] == 5
    assert stage["date"] == "2026-08-25"
    assert stage["profile_type"] == "mountain"
    assert stage["distance_km"] == 184.7
    assert stage["vertical_meters"] == 3650
    assert stage["finish_type"] == "summit"
    assert stage["startlist_count"] == 3
    assert stage["result_count"] == 3
    assert [row["rider_slug"] for row in stage["startlist"]] == [
        "jonas-vingegaard",
        "remco-evenepoel",
        "tim-merlier",
    ]
    assert stage["results"][0] == {
        "rank": 1,
        "rider": "Jonas Vingegaard",
        "rider_slug": "jonas-vingegaard",
        "team": "Visma | Lease a Bike",
        "time": "4:22:10",
    }
    assert stage["results"][2]["rider"] == "Primož Roglič"


def test_parse_startlist_fixture() -> None:
    html = (FIXTURES / "startlist.html").read_text(encoding="utf-8")
    startlist = parse_startlist(html)

    assert startlist == [
        {"rider": "Tadej Pogačar", "rider_slug": "tadej-pogacar", "team": "UAE Team Emirates"},
        {"rider": "Juan Ayuso", "rider_slug": "juan-ayuso", "team": "UAE Team Emirates"},
    ]


def test_parse_live_pcs_list_startlist_excludes_sidebar_riders() -> None:
    html = """
    <html><body>
      <ul class="startlist_v4">
        <li>
          <div class="ridersCont">
            <a class="team" href="team/uae-2026">UAE Team Emirates - XRG (WT)</a>
            <ul>
              <li><a href="rider/tadej-pogacar">POGAČAR Tadej</a></li>
              <li><a href="rider/joao-almeida">ALMEIDA João</a></li>
            </ul>
          </div>
        </li>
      </ul>
      <aside><a href="rider/demi-vollering">Demi Vollering</a></aside>
    </body></html>
    """

    assert parse_startlist(html) == [
        {
            "rider": "POGAČAR Tadej",
            "rider_slug": "tadej-pogacar",
            "team": "UAE Team Emirates - XRG (WT)",
        },
        {
            "rider": "ALMEIDA João",
            "rider_slug": "joao-almeida",
            "team": "UAE Team Emirates - XRG (WT)",
        },
    ]


def test_parse_live_pcs_profile_details() -> None:
    html = """
    <html><head><title>La Vuelta 2026 Stage 20</title></head><body>
      <h1>La Vuelta Ciclista a España</h1>
      <div>Stage 20</div>
      <ul>
        <li><div class="title">Distance: </div><div class="value">206.7 km</div></li>
        <li><div class="title">Parcours type: </div>
            <div class="value"><span class="icon profile p5"></span></div></li>
        <li><div class="title">Gradient final km: </div><div class="value">10.3%</div></li>
        <li><div class="title">ProfileScore: </div><div class="value">526</div></li>
        <li><div class="title">Vertical meters: </div><div class="value">5041</div></li>
        <li><div class="title">Race ranking: </div><div class="value">12</div></li>
        <li><div class="title">Startlist quality score: </div><div class="value">987 (876)</div></li>
        <li><div class="title">Departure: </div><div class="value">La Calahorra</div></li>
        <li><div class="title">Arrival: </div><div class="value">Collado del Alguacil</div></li>
      </ul>
    </body></html>
    """

    stage = parse_stage_page(html)

    assert stage["profile_type"] == "mountain"
    assert stage["finish_type"] == "summit"
    assert stage["vertical_meters"] == 5041
    assert stage["profile_score"] == 526
    assert stage["gradient_final_km"] == 10.3
    assert stage["race_ranking"] == 12
    assert stage["startlist_quality_score"] == 987
    assert stage["startlist_quality_finish_score"] == 876
    assert stage["startlist_count"] == 0
    assert stage["departure"] == "La Calahorra"
    assert stage["arrival"] == "Collado del Alguacil"
