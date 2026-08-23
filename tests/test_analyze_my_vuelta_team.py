from scripts.analyze_my_vuelta_team import _lineup_sort_key


def test_lineup_sort_ignores_conditional_team_bonuses() -> None:
    conditional_favorite = (100.0, 0.0, 999, "Bonus", (), 1, 1.0)
    objective_favorite = (10.0, 10.0, 1, "Scorer", (), 2, 5.0)
    objective_tiebreak = (0.0, 0.0, 999, "Fallback", (), 3, 2.0)

    ranked = sorted(
        [conditional_favorite, objective_favorite, objective_tiebreak],
        key=_lineup_sort_key,
    )

    assert ranked == [
        objective_favorite,
        objective_tiebreak,
        conditional_favorite,
    ]
