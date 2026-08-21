from scorito_agent.pcs.slugs import RIDER_SLUG_EXCEPTIONS, ascii_slug, slugify_rider


def test_slugify_default_ascii_rule() -> None:
    assert ascii_slug("Tadej Pogačar") == "tadej-pogacar"
    assert slugify_rider({"FirstName": "Remco", "LastName": "Evenepoel"}) == "remco-evenepoel"


def test_slugify_reference_exceptions() -> None:
    assert len(RIDER_SLUG_EXCEPTIONS) == 14
    assert slugify_rider("Chris", "Froome") == "christopher-froome"
    assert slugify_rider("Mikkel", "Frølich Honoré") == "mikkel-honore"
    assert slugify_rider("Søren", "Kragh") == "soren-kragh-andersen"
    assert slugify_rider({"FirstName": "Georg", "LastName": "Zimmerman"}) == "georg-zimmermann"
