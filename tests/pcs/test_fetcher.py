from scorito_agent.pcs.fetcher import (
    URL_TEMPLATES,
    race_page_url,
    rider_page_url,
    rider_search_url,
    stage_page_url,
    stage_startlist_url,
)


def test_url_templates_and_builders() -> None:
    assert URL_TEMPLATES["rider"] == "https://www.procyclingstats.com/rider/{rider_slug}"
    assert rider_page_url("Chris", "Froome") == "https://www.procyclingstats.com/rider/christopher-froome"
    assert rider_search_url("Tadej Pogačar").startswith(
        "https://www.procyclingstats.com/resources/search.php?searchfrom=&term="
    )
    assert race_page_url("vuelta-a-espana", 2025) == "https://www.procyclingstats.com/race/vuelta-a-espana/2025"
    assert stage_page_url("vuelta-a-espana", 2025, 7) == "https://www.procyclingstats.com/race/vuelta-a-espana/2025/stage-7"
    assert stage_startlist_url("vuelta-a-espana", 2025, "stage-7") == "https://www.procyclingstats.com/race/vuelta-a-espana/2025/stage-7/startlist"
