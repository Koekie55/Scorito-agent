"""Behavior tests for the evidence-aware expert-chat pipeline."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from scorito_agent.expert_chat import (
    ChatMessage,
    ClaimAction,
    ClaimCategory,
    ClaimLifecycle,
    EvidenceTier,
    ExpertChatStore,
    RiderNote,
    apply_signal,
    build_rider_aliases,
    classify_action,
    classify_category,
    extract_stages,
    find_riders,
    name_key,
    parse_export,
    sentiment_score,
    signals_by_rider_stage,
)

RIDERS = [
    "Jonas Vingegaard",
    "Jay Vine",
    "Wout van Aert",
    "Mads Pedersen",
    "Tadej Pogačar",
]


def _import(
    tmp_path,
    export: str,
    *,
    now: datetime = datetime(2026, 8, 2, tzinfo=UTC),
) -> ExpertChatStore:
    store = ExpertChatStore(tmp_path)
    store.import_messages(parse_export(export), RIDERS, now=now)
    return store


def test_parses_multiline_whatsapp_with_aware_timestamps_and_source_lines() -> None:
    export = """\
[01/08/2026, 09:33:44] ~ Hemmo: Wout van Aert is sterk in rit 2
Tweede regel met uitleg
[01/08/2026, 09:34:44] ~ Hemmo: Wout van Aert is sterk in rit 2
"""

    first, second = parse_export(export)

    assert first.author_key == "hemmo"
    assert first.author_display == "~ Hemmo"
    assert first.timestamp == datetime(2026, 8, 1, 9, 33, 44, tzinfo=UTC)
    assert first.line_number == 1
    assert first.text.endswith("Tweede regel met uitleg")
    assert second.line_number == 3
    assert first.message_id != second.message_id


def test_parses_smiley_author_with_internal_colon_as_separate_messages() -> None:
    export = (
        "[14/08/2026, 11:09:37] ~\u202fZwetmas: Eerste\n"
        "[14/08/2026, 11:09:55] ~\u202f:): Tweede\n"
        "[14/08/2026, 11:10:44] ~\u202f:): Derde"
    )

    parsed = parse_export(export)

    assert [(message.author_key, message.text) for message in parsed] == [
        ("zwetmas", "Eerste"),
        (":)", "Tweede"),
        (":)", "Derde"),
    ]
    assert parsed[1].speaker.profile_id == "smiley"
    assert parsed[1].speaker.t3_factor == pytest.approx(0.76)


def test_parse_structured_export_keeps_colon_lines_in_message_body() -> None:
    messages = parse_export(
        "\u200e[01/08/2026, 10:00:00] +31 6 12345678: https://example.com/rider\n"
        "\u200e[01/08/2026, 10:01:00] Hemmo: Poll:\n"
        "OPTION: Van Aert\n"
        "https://example.com/stage\n"
        "Sprint: Milan\n"
        "Vine: useful on this profile"
    )

    assert len(messages) == 2
    assert messages[0].author_display == "+31 6 12345678"
    assert messages[0].text == "https://example.com/rider"
    assert messages[1].author_display == "Hemmo"
    assert messages[1].text == (
        "Poll:\n"
        "OPTION: Van Aert\n"
        "https://example.com/stage\n"
        "Sprint: Milan\n"
        "Vine: useful on this profile"
    )


def test_parse_structured_export_accepts_empty_decorated_smiley_header() -> None:
    messages = parse_export(
        "\u200e[30-7-2025, 13:46:35] \u200e~\u202f:):\n"
        "\u200e[30-7-2025, 13:47:01] \u200e~\u202f:): image omitted"
    )

    assert len(messages) == 2
    assert [message.speaker.profile_id for message in messages] == ["smiley", "smiley"]
    assert [message.text for message in messages] == ["", "image omitted"]


def test_parse_simple_only_export_still_supports_name_colon_messages() -> None:
    messages = parse_export(
        "Hemmo: Van Aert is a credible bunch sprinter\n"
        "Quinten: Agreed"
    )

    assert len(messages) == 2
    assert [message.author_display for message in messages] == ["Hemmo", "Quinten"]
    assert [message.text for message in messages] == [
        "Van Aert is a credible bunch sprinter",
        "Agreed",
    ]


def test_message_and_note_serialization_round_trip_is_deterministic() -> None:
    message = parse_export(
        "[01/08/2026, 09:33:44] Hemmo: Volgens PCS is Wout van Aert sterk"
    )[0]
    assert ChatMessage.from_dict(message.to_dict()).to_dict() == message.to_dict()

    note = RiderNote(
        note_id="n1",
        message_id=message.message_id,
        rider_key=name_key("Wout van Aert"),
        rider_name="Wout van Aert",
        author_key=message.author_key,
        author_display=message.author_display,
        timestamp=message.timestamp,
        source_line=message.line_number,
        text=message.text,
        category=ClaimCategory.FORM,
        action=ClaimAction.ASSERT,
        evidence_tier=EvidenceTier.T3,
        lifecycle=ClaimLifecycle.ACTIVE,
        sentiment=0.6,
        extraction_confidence=0.9,
        speaker_factor=0.78,
        list_discount=1.0,
        rider_count_in_message=1,
        stages=(2,),
        sources=("https://www.procyclingstats.com/",),
        source_message_ids=("source-1",),
    )
    assert RiderNote.from_dict(note.to_dict()).to_dict() == note.to_dict()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("gaat voor rit 6 en 7", (6, 7)),
        ("mikt op rit 9 t/m 11", (9, 10, 11)),
        ("stage 21 is de sprint", (21,)),
        ("rit 40 bestaat niet", ()),
    ],
)
def test_stage_references_are_extracted(text: str, expected: tuple[int, ...]) -> None:
    assert extract_stages(text) == expected


def test_unique_surnames_match_but_full_names_win() -> None:
    aliases = build_rider_aliases(RIDERS)
    assert find_riders("Vine gaat vroeg mee", aliases) == ["Jay Vine"]
    assert find_riders("Wout van Aert is ziek", aliases) == ["Wout van Aert"]


def test_dutch_and_english_negation_overrides_negative_health_terms() -> None:
    assert sentiment_score("Wout van Aert is ziek en geblesseerd") < 0
    assert sentiment_score("Wout van Aert is niet ziek en heeft geen blessure") > 0
    assert sentiment_score("Wout van Aert is not sick and has no injury") > 0
    assert classify_action("Correction: he is not sick") is ClaimAction.CORRECT
    assert classify_category("He will not start because of an injury") is ClaimCategory.AVAILABILITY


def test_nearby_same_author_url_sets_provenance_and_evidence_tier(tmp_path) -> None:
    export = """\
[01/08/2026, 09:00:00] Analyst: https://www.procyclingstats.com/rider/wout-van-aert
[01/08/2026, 09:05:00] Analyst: Based on PCS results, Wout van Aert is strong in stage 2
[01/08/2026, 10:00:00] Reporter: https://www.teamvismaleaseabike.com/news/team-update
[01/08/2026, 10:04:00] Reporter: Team update: Wout van Aert will start stage 2
"""

    store = _import(tmp_path, export)
    pcs_note = next(note for note in store.notes if note.author_key == "analyst")
    official_note = next(note for note in store.notes if note.author_key == "reporter")

    assert pcs_note.evidence_tier is EvidenceTier.T2
    assert pcs_note.sources == ("https://www.procyclingstats.com/rider/wout-van-aert",)
    assert pcs_note.source_message_ids
    assert official_note.evidence_tier is EvidenceTier.T1
    assert official_note.speaker_factor == 1.0


def test_correction_supersedes_prior_claim_and_links_provenance(tmp_path) -> None:
    export = """\
[01/08/2026, 09:00:00] Analyst: I think Wout van Aert is sick
[01/08/2026, 09:10:00] Analyst: Correction based on the team update: Wout van Aert is not sick
"""

    store = _import(tmp_path, export)
    prior, correction = sorted(store.notes, key=lambda note: note.timestamp)

    assert prior.lifecycle is ClaimLifecycle.SUPERSEDED
    assert prior.superseded_by == correction.note_id
    assert correction.lifecycle is ClaimLifecycle.ACTIVE
    assert correction.action is ClaimAction.CORRECT
    assert store.rider_signal("Wout van Aert") > 0


def test_retraction_removes_claim_from_model(tmp_path) -> None:
    export = """\
[01/08/2026, 09:00:00] Analyst: I think Jay Vine is strong in stage 6
[01/08/2026, 09:10:00] Analyst: I retract: Jay Vine is strong in stage 6
"""

    store = _import(tmp_path, export)

    assert all(note.lifecycle is ClaimLifecycle.RETRACTED for note in store.notes)
    assert store.rider_signal("Jay Vine", 6) == 0.0


def test_equal_tier_opposing_claims_cancel_and_link_conflicts(tmp_path) -> None:
    export = """\
[01/08/2026, 09:00:00] Analyst A: I think Mads Pedersen is strong in stage 2
[01/08/2026, 09:10:00] Analyst B: In my view Mads Pedersen is out of form in stage 2
"""

    store = _import(tmp_path, export)

    assert {note.lifecycle for note in store.notes} == {ClaimLifecycle.CONTRADICTORY}
    assert all(note.conflicts_with for note in store.notes)
    assert store.rider_signal("Mads Pedersen", 2) == 0.0


def test_expired_form_claim_becomes_stale(tmp_path) -> None:
    export = (
        "[01/07/2026, 09:00:00] Analyst: "
        "Based on recent results, Wout van Aert is in good form"
    )
    store = _import(
        tmp_path,
        export,
        now=datetime(2026, 8, 2, 9, 1, tzinfo=UTC),
    )

    assert store.notes[0].lifecycle is ClaimLifecycle.STALE
    assert not store.notes[0].contributes_to_model


def test_humour_is_preserved_but_never_affects_signals(tmp_path) -> None:
    export = (
        "[01/08/2026, 09:00:00] Joker: "
        "I think Jay Vine wins every stage 😂 haha, absolute zekerheid"
    )
    store = _import(tmp_path, export)

    assert store.notes[0].evidence_tier is EvidenceTier.HUMOUR
    assert store.notes[0].category is ClaimCategory.HUMOUR
    assert store.rider_signal("Jay Vine") == 0.0


def test_repetition_does_not_manufacture_authority(tmp_path) -> None:
    single = _import(
        tmp_path / "single",
        "[01/08/2026, 09:00:00] Analyst A: "
        "Based on recent results, Wout van Aert is strong",
    )
    repeated = _import(
        tmp_path / "repeated",
        """\
[01/08/2026, 09:00:00] Analyst A: Based on recent results, Wout van Aert is strong
[01/08/2026, 09:01:00] Analyst B: Based on recent results, Wout van Aert is strong
[01/08/2026, 09:02:00] Analyst C: Based on recent results, Wout van Aert is strong
""",
    )

    assert repeated.rider_signal("Wout van Aert") == single.rider_signal("Wout van Aert")
    assert sum(note.contributes_to_model for note in repeated.notes) == 1


def test_oversized_rider_lists_are_discounted(tmp_path) -> None:
    single = _import(
        tmp_path / "single",
        "[01/08/2026, 09:00:00] Analyst: "
        "Based on results, Wout van Aert is strong",
    )
    oversized = _import(
        tmp_path / "oversized",
        "[01/08/2026, 09:00:00] Analyst: Based on results, "
        "Wout van Aert, Mads Pedersen, Jay Vine, Jonas Vingegaard and "
        "Tadej Pogačar are strong",
    )

    assert oversized.notes[0].rider_count_in_message == 5
    assert oversized.notes[0].list_discount == pytest.approx(1 / 5**0.5)
    assert oversized.notes[0].effective_weight < single.notes[0].effective_weight


def test_signal_api_is_bounded_and_applied_once(tmp_path) -> None:
    store = _import(
        tmp_path,
        "[01/08/2026, 09:00:00] Analyst: "
        "Based on recent results, Wout van Aert is strong in stage 2",
    )

    signal = signals_by_rider_stage(
        store.build_digest(),
        stage_max=3,
    )[name_key("Wout van Aert")][2]
    assert 0.0 < signal <= 1.0
    assert apply_signal(100.0, signal) == pytest.approx(100.0 * (1.0 + signal * 0.12))
    assert apply_signal(100.0, 99.0) == pytest.approx(112.0)
    assert apply_signal(100.0, -99.0) == pytest.approx(88.0)
    with pytest.raises(ValueError):
        apply_signal(100.0, signal, max_adjustment=0.13)


def test_persistence_and_reimport_are_idempotent(tmp_path) -> None:
    export = """\
[01/08/2026, 09:00:00] Analyst: Based on results, Wout van Aert is strong in stage 2
[01/08/2026, 09:01:00] Analyst: Mads Pedersen is too expensive
"""
    messages = parse_export(export)
    store = ExpertChatStore(tmp_path)

    first = store.import_messages(messages, RIDERS)
    reloaded = ExpertChatStore(tmp_path)
    second = reloaded.import_messages(messages, RIDERS)

    assert first["messages_added"] == 2
    assert second["messages_added"] == 0
    assert [message.to_dict() for message in reloaded.messages] == [
        message.to_dict() for message in store.messages
    ]
    assert [note.note_id for note in reloaded.notes] == [note.note_id for note in store.notes]
    assert len((tmp_path / "raw_messages.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_rebuild_replaces_legacy_message_ids_instead_of_merging(tmp_path) -> None:
    export = """\
[01/08/2026, 09:00:00] Analyst: Based on results, Wout van Aert is strong in stage 2
[01/08/2026, 09:01:00] Analyst: Mads Pedersen is too expensive
"""
    canonical = parse_export(export)
    legacy = replace(canonical[0], message_id="legacy-parser-message-id")
    store = ExpertChatStore(tmp_path)
    store.import_messages([legacy], RIDERS)

    rebuilt = store.import_messages(canonical, RIDERS, replace_existing=True)
    reloaded = ExpertChatStore(tmp_path)
    repeated = reloaded.import_messages(canonical, RIDERS)

    assert rebuilt == {
        "messages_seen": 2,
        "messages_added": 2,
        "messages_total": 2,
        "notes_total": 2,
    }
    assert repeated["messages_added"] == 0
    assert [message.message_id for message in reloaded.messages] == [
        message.message_id for message in canonical
    ]
    assert all(note.message_id != legacy.message_id for note in reloaded.notes)


def test_repeated_identical_events_are_preserved_and_reimport_is_idempotent(
    tmp_path,
) -> None:
    export = (
        "[10-08-2026, 12:11:11] ~ Luke: image omitted\n"
        "[10-08-2026, 12:11:11] ~ Luke: image omitted\n"
        "[10-08-2026, 12:11:11] ~ Luke: image omitted\n"
    )

    messages = parse_export(export)
    reparsed = parse_export(export)

    assert len(messages) == 3
    assert len({message.message_id for message in messages}) == 3
    assert [message.message_id for message in reparsed] == [
        message.message_id for message in messages
    ]

    store = ExpertChatStore(tmp_path)
    first = store.import_messages(messages, RIDERS)
    second = store.import_messages(reparsed, RIDERS)

    assert first["messages_added"] == 3
    assert second["messages_added"] == 0
    assert len(store.messages) == 3


def test_digest_exposes_only_active_model_signals(tmp_path) -> None:
    store = _import(
        tmp_path,
        """\
[01/08/2026, 09:00:00] Analyst: Based on results, Jay Vine is strong in stage 6
[01/08/2026, 09:10:00] Joker: Jay Vine wins them all 😂 haha
""",
    )

    digest = store.build_digest(generated_at=datetime(2026, 8, 2, tzinfo=UTC))
    vine = digest["riders"][name_key("Jay Vine")]

    assert digest["schema_version"] == 2
    assert digest["model_contract"]["allowed_evidence_tiers"] == ["T1", "T2", "T3"]
    assert vine["stage_signals"]["6"] > 0
    assert vine["active_model_claim_count"] == 1
    assert any(note["evidence_tier"] == "H" for note in vine["notes"])
