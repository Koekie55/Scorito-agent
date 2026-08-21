"""Persistent evidence store and bounded rider-signal aggregation."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .extract import (
    build_rider_aliases,
    claim_fingerprint,
    classify_action,
    classify_category,
    classify_evidence_tier,
    extract_stages,
    extract_urls,
    extraction_confidence,
    find_riders,
    is_url_only_message,
    name_key,
    sentiment_score,
)
from .models import (
    EVIDENCE_WEIGHTS,
    LIFECYCLE_EXPIRY_DAYS,
    ChatMessage,
    ClaimAction,
    ClaimCategory,
    ClaimLifecycle,
    EvidenceTier,
    RiderNote,
    speaker_profile,
)

SCHEMA_VERSION = 2
MODEL_TIERS = frozenset({EvidenceTier.T1, EvidenceTier.T2, EvidenceTier.T3})
SIGNAL_MIN = -1.0
SIGNAL_MAX = 1.0
CONSUMER_MAX_ADJUSTMENT = 0.12
SOURCE_ADJACENCY_LIMIT = 2
SOURCE_ADJACENCY_WINDOW = timedelta(minutes=15)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    text = str(value).strip()
    if not text:
        return datetime(1970, 1, 1, tzinfo=UTC)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return _as_utc(datetime.fromisoformat(text))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=UTC)


def _note_sort_key(note: RiderNote) -> tuple[datetime, int, str]:
    return _timestamp(note.timestamp), note.source_line or 0, note.note_id


def _evidence_rank(tier: EvidenceTier) -> int:
    return {
        EvidenceTier.HUMOUR: 0,
        EvidenceTier.T4: 1,
        EvidenceTier.T3: 2,
        EvidenceTier.T2: 3,
        EvidenceTier.T1: 4,
    }[tier]


def _claim_strength(note: RiderNote) -> tuple[int, float, datetime, str]:
    return (
        _evidence_rank(note.evidence_tier),
        note.extraction_confidence * note.list_discount,
        _timestamp(note.timestamp),
        note.note_id,
    )


def _list_discount(rider_count: int) -> float:
    if rider_count <= 1:
        return 1.0
    return max(0.40, 1.0 / math.sqrt(rider_count))


def _note_id(
    message_id: str,
    rider_key: str,
    category: ClaimCategory,
    stages: tuple[int, ...],
) -> str:
    material = "\x1f".join((message_id, rider_key, category.value, ",".join(map(str, stages))))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
            handle.write("\n")
    os.replace(temporary, path)


class ExpertChatStore:
    """Incremental raw-message store with deterministic claim rebuilding."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.messages_path = self.root / "raw_messages.jsonl"
        self.notes_path = self.root / "notes.jsonl"
        self.index_path = self.root / "riders.json"
        self.metadata_path = self.root / "metadata.json"
        self._messages: dict[str, ChatMessage] = {}
        self._notes: dict[str, RiderNote] = {}
        self._load()

    @property
    def messages(self) -> tuple[ChatMessage, ...]:
        return tuple(sorted(self._messages.values(), key=lambda item: (item.timestamp, item.line_number, item.message_id)))

    @property
    def notes(self) -> tuple[RiderNote, ...]:
        return tuple(sorted(self._notes.values(), key=_note_sort_key))

    def _load(self) -> None:
        if self.messages_path.exists():
            for line in self.messages_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    message = ChatMessage.from_dict(json.loads(line))
                except (KeyError, TypeError, ValueError):
                    continue
                self._messages[message.message_id] = message
        if self.notes_path.exists():
            for line in self.notes_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    note = RiderNote.from_dict(json.loads(line))
                except (KeyError, TypeError, ValueError):
                    continue
                self._notes[note.note_id] = note

    def import_messages(
        self,
        messages: Iterable[ChatMessage],
        rider_names: Iterable[str],
        *,
        now: datetime | None = None,
        replace_existing: bool = False,
    ) -> dict[str, int]:
        incoming = list(messages)
        if replace_existing:
            self._messages.clear()
            self._notes.clear()
        before = len(self._messages)
        for message in incoming:
            self._messages[message.message_id] = message
        aliases = build_rider_aliases(rider_names)
        self._notes = {
            note.note_id: note
            for note in self._resolve_lifecycle(
                self._extract_notes(aliases),
                now=_as_utc(now) if now else _utc_now(),
            )
        }
        self._write()
        return {
            "messages_seen": len(incoming),
            "messages_added": len(self._messages) - before,
            "messages_total": len(self._messages),
            "notes_total": len(self._notes),
        }

    def _source_provenance(self) -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
        """Attach up to two nearby same-author source messages within 15 minutes."""

        by_author: dict[str, list[ChatMessage]] = defaultdict(list)
        for message in self.messages:
            by_author[message.author_key].append(message)

        provenance: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
        for author_messages in by_author.values():
            for index, message in enumerate(author_messages):
                urls = list(extract_urls(message.text))
                source_ids: list[str] = [message.message_id] if urls else []
                neighbours: list[tuple[timedelta, ChatMessage]] = []
                for candidate_index in range(max(0, index - 2), min(len(author_messages), index + 3)):
                    if candidate_index == index:
                        continue
                    candidate = author_messages[candidate_index]
                    candidate_urls = extract_urls(candidate.text)
                    if not candidate_urls:
                        continue
                    delta = abs(_as_utc(candidate.timestamp) - _as_utc(message.timestamp))
                    if delta <= SOURCE_ADJACENCY_WINDOW:
                        neighbours.append((delta, candidate))
                for _, candidate in sorted(
                    neighbours,
                    key=lambda item: (item[0], abs(author_messages.index(item[1]) - index)),
                )[:SOURCE_ADJACENCY_LIMIT]:
                    source_ids.append(candidate.message_id)
                    urls.extend(extract_urls(candidate.text))
                provenance[message.message_id] = (
                    tuple(dict.fromkeys(urls)),
                    tuple(dict.fromkeys(source_ids)),
                )
        return provenance

    def _extract_notes(
        self,
        aliases: Mapping[str, tuple[str, ...]],
    ) -> list[RiderNote]:
        provenance = self._source_provenance()
        notes: list[RiderNote] = []
        for message in self.messages:
            if is_url_only_message(message.text):
                continue
            riders = find_riders(message.text, aliases)
            if not riders:
                continue
            stages = extract_stages(message.text)
            category = classify_category(message.text, stages)
            action = classify_action(message.text)
            sources, source_message_ids = provenance.get(message.message_id, ((), ()))
            tier = classify_evidence_tier(
                message.text,
                sources=sources,
                category=category,
            )
            sentiment = sentiment_score(message.text)
            confidence = extraction_confidence(
                rider_count=len(riders),
                category=category,
                stages=stages,
                sentiment=sentiment,
            )
            list_discount = _list_discount(len(riders))
            profile = speaker_profile(message.author_key)
            for rider_name in riders:
                rider_key = name_key(rider_name)
                notes.append(
                    RiderNote(
                        note_id=_note_id(message.message_id, rider_key, category, stages),
                        message_id=message.message_id,
                        rider_key=rider_key,
                        rider_name=rider_name,
                        author_key=message.author_key,
                        author_display=message.author_display,
                        timestamp=message.timestamp,
                        source_line=message.line_number,
                        text=message.text,
                        category=category,
                        action=action,
                        evidence_tier=tier,
                        lifecycle=ClaimLifecycle.ACTIVE,
                        sentiment=sentiment,
                        extraction_confidence=confidence,
                        speaker_factor=profile.factor_for(tier),
                        list_discount=list_discount,
                        rider_count_in_message=len(riders),
                        stages=stages,
                        sources=sources,
                        source_message_ids=source_message_ids,
                    )
                )
        return notes

    def _resolve_lifecycle(
        self,
        notes: list[RiderNote],
        *,
        now: datetime,
    ) -> list[RiderNote]:
        resolved = {note.note_id: note for note in notes}

        # Corrections and retractions follow the author's prior claim even if the
        # correction changes the original stage scope.
        by_subject: dict[tuple[str, ClaimCategory], list[RiderNote]] = defaultdict(list)
        for note in notes:
            by_subject[(note.rider_key, note.category)].append(note)
        for subject_notes in by_subject.values():
            ordered = sorted(subject_notes, key=_note_sort_key)
            for index, note in enumerate(ordered):
                if note.action not in {ClaimAction.CORRECT, ClaimAction.RETRACT}:
                    continue
                candidates = [
                    resolved[prior.note_id]
                    for prior in ordered[:index]
                    if prior.author_key == note.author_key
                    and resolved[prior.note_id].lifecycle == ClaimLifecycle.ACTIVE
                ]
                if not candidates:
                    candidates = [
                        resolved[prior.note_id]
                        for prior in ordered[:index]
                        if resolved[prior.note_id].lifecycle == ClaimLifecycle.ACTIVE
                    ]
                if candidates:
                    prior = max(candidates, key=_note_sort_key)
                    prior_state = (
                        ClaimLifecycle.RETRACTED
                        if note.action == ClaimAction.RETRACT
                        else ClaimLifecycle.SUPERSEDED
                    )
                    resolved[prior.note_id] = replace(
                        prior,
                        lifecycle=prior_state,
                        superseded_by=note.note_id,
                    )
                if note.action == ClaimAction.RETRACT:
                    resolved[note.note_id] = replace(note, lifecycle=ClaimLifecycle.RETRACTED)

        grouped: dict[
            tuple[str, ClaimCategory, tuple[int, ...]],
            list[RiderNote],
        ] = defaultdict(list)
        for note in resolved.values():
            grouped[claim_fingerprint(note.rider_key, note.category, note.stages)].append(note)

        for group_notes in grouped.values():
            active = [
                resolved[note.note_id]
                for note in group_notes
                if resolved[note.note_id].lifecycle == ClaimLifecycle.ACTIVE
                and resolved[note.note_id].action != ClaimAction.RETRACT
            ]
            if not active:
                continue
            positive = [note for note in active if note.sentiment > 0]
            negative = [note for note in active if note.sentiment < 0]
            neutral = [note for note in active if note.sentiment == 0]

            for note in neutral:
                if note.evidence_tier == EvidenceTier.T4 and any(
                    marker in note.text.lower()
                    for marker in ("onzeker", "mogelijk", "misschien", "gerucht", "voorlopig")
                ):
                    resolved[note.note_id] = replace(note, lifecycle=ClaimLifecycle.UNRESOLVED)

            if positive and negative:
                best_positive = max(positive, key=_claim_strength)
                best_negative = max(negative, key=_claim_strength)
                positive_rank = _evidence_rank(best_positive.evidence_tier)
                negative_rank = _evidence_rank(best_negative.evidence_tier)
                if positive_rank == negative_rank:
                    for note in positive + negative:
                        opposing = negative if note.sentiment > 0 else positive
                        resolved[note.note_id] = replace(
                            note,
                            lifecycle=(
                                ClaimLifecycle.CONTRADICTORY
                                if _evidence_rank(note.evidence_tier) == positive_rank
                                else ClaimLifecycle.SUPERSEDED
                            ),
                            conflicts_with=tuple(
                                sorted(
                                    opposing_note.note_id
                                    for opposing_note in opposing
                                    if _evidence_rank(opposing_note.evidence_tier)
                                    == positive_rank
                                )
                            ),
                        )
                else:
                    winning = positive if positive_rank > negative_rank else negative
                    losing = negative if positive_rank > negative_rank else positive
                    winner = max(winning, key=_claim_strength)
                    for note in winning:
                        if note.note_id != winner.note_id:
                            resolved[note.note_id] = replace(
                                note,
                                lifecycle=ClaimLifecycle.SUPERSEDED,
                                superseded_by=winner.note_id,
                            )
                    for note in losing:
                        resolved[note.note_id] = replace(
                            note,
                            lifecycle=ClaimLifecycle.SUPERSEDED,
                            superseded_by=winner.note_id,
                        )
            else:
                same_direction = positive or negative
                if same_direction:
                    winner = max(same_direction, key=_claim_strength)
                    for note in same_direction:
                        if note.note_id != winner.note_id:
                            resolved[note.note_id] = replace(
                                note,
                                lifecycle=ClaimLifecycle.SUPERSEDED,
                                superseded_by=winner.note_id,
                            )

        for note_id, note in tuple(resolved.items()):
            if note.lifecycle != ClaimLifecycle.ACTIVE:
                continue
            expiry_days = LIFECYCLE_EXPIRY_DAYS.get(note.category)
            if expiry_days is None:
                continue
            if now - _as_utc(note.timestamp) > timedelta(days=expiry_days):
                resolved[note_id] = replace(note, lifecycle=ClaimLifecycle.STALE)

        return sorted(resolved.values(), key=_note_sort_key)

    def _write(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_jsonl(self.messages_path, (message.to_dict() for message in self.messages))
        _atomic_jsonl(self.notes_path, (note.to_dict() for note in self.notes))
        rider_index: dict[str, list[str]] = defaultdict(list)
        for note in self.notes:
            rider_index[note.rider_key].append(note.note_id)
        _atomic_json(self.index_path, dict(sorted(rider_index.items())))
        _atomic_json(
            self.metadata_path,
            {
                "schema_version": SCHEMA_VERSION,
                "messages": len(self._messages),
                "notes": len(self._notes),
                "updated_at": _utc_now().isoformat(),
            },
        )

    def notes_for_rider(self, rider: str) -> tuple[RiderNote, ...]:
        rider_key = name_key(rider)
        return tuple(note for note in self.notes if note.rider_key == rider_key)

    @staticmethod
    def _strongest_per_category(notes: Iterable[RiderNote]) -> list[RiderNote]:
        strongest: dict[ClaimCategory, RiderNote] = {}
        for note in notes:
            if not note.contributes_to_model:
                continue
            current = strongest.get(note.category)
            if current is None or _claim_strength(note) > _claim_strength(current):
                strongest[note.category] = note
        return list(strongest.values())

    @staticmethod
    def _signal(notes: Iterable[RiderNote]) -> float:
        selected = ExpertChatStore._strongest_per_category(notes)
        signal = sum(note.sentiment * note.effective_weight for note in selected)
        return max(SIGNAL_MIN, min(SIGNAL_MAX, signal))

    def rider_signal(self, rider: str, stage: int | None = None) -> float:
        notes = self.notes_for_rider(rider)
        if stage is not None:
            notes = tuple(note for note in notes if not note.stages or stage in note.stages)
        return self._signal(notes)

    def build_digest(self, *, generated_at: datetime | None = None) -> dict[str, Any]:
        generated = _as_utc(generated_at) if generated_at else _utc_now()
        by_rider: dict[str, list[RiderNote]] = defaultdict(list)
        for note in self.notes:
            by_rider[note.rider_key].append(note)

        riders: dict[str, dict[str, Any]] = {}
        stage_intent: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rider_key, rider_notes in sorted(by_rider.items()):
            overall_signal = self._signal(rider_notes)
            stages = sorted({stage for note in rider_notes for stage in note.stages})
            stage_signals = {
                str(stage): self._signal(
                    note for note in rider_notes if not note.stages or stage in note.stages
                )
                for stage in stages
            }
            active_claims = [
                note
                for note in rider_notes
                if note.lifecycle == ClaimLifecycle.ACTIVE
                and note.evidence_tier in MODEL_TIERS
            ]
            riders[rider_key] = {
                "name": rider_notes[-1].rider_name,
                "signal": overall_signal,
                "bias": overall_signal,
                "max_model_adjustment": overall_signal * CONSUMER_MAX_ADJUSTMENT,
                "stage_signals": stage_signals,
                "claim_count": len(rider_notes),
                "active_model_claim_count": sum(note.contributes_to_model for note in rider_notes),
                "categories": sorted({note.category.value for note in active_claims}),
                "latest_claim_at": max(note.timestamp for note in rider_notes).isoformat(),
                "notes": [note.to_dict() for note in sorted(rider_notes, key=_note_sort_key)],
            }
            for note in active_claims:
                if note.category != ClaimCategory.STAGE_INTENT:
                    continue
                for stage in note.stages:
                    stage_intent[str(stage)].append(
                        {
                            "rider_key": rider_key,
                            "rider_name": note.rider_name,
                            "signal": note.sentiment * note.effective_weight,
                            "evidence_tier": note.evidence_tier.value,
                            "message_id": note.message_id,
                            "sources": list(note.sources),
                        }
                    )

        speaker_stats: dict[str, dict[str, Any]] = {}
        messages_by_author = Counter(message.author_key for message in self.messages)
        for author_key in sorted(messages_by_author):
            author_notes = [note for note in self.notes if note.author_key == author_key]
            profile = speaker_profile(author_key)
            speaker_stats[author_key] = {
                "messages": messages_by_author[author_key],
                "claims": len(author_notes),
                "active_model_claims": sum(note.contributes_to_model for note in author_notes),
                "evidence_tiers": dict(Counter(note.evidence_tier.value for note in author_notes)),
                "lifecycles": dict(Counter(note.lifecycle.value for note in author_notes)),
                "calibration": {
                    "label": profile.label,
                    "t3_factor": profile.t3_factor,
                    "oversized_list_prone": profile.oversized_list_prone,
                    "strengths": list(profile.strengths),
                    "cautions": list(profile.cautions),
                },
            }

        lifecycle_counts = Counter(note.lifecycle.value for note in self.notes)
        tier_counts = Counter(note.evidence_tier.value for note in self.notes)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated.isoformat(),
            "model_contract": {
                "signal_range": [SIGNAL_MIN, SIGNAL_MAX],
                "consumer_formula": "multiplier = 1 + signal * 0.12",
                "max_adjustment": CONSUMER_MAX_ADJUSTMENT,
                "application_count": 1,
                "model_tiers": [tier.value for tier in (EvidenceTier.T1, EvidenceTier.T2, EvidenceTier.T3)],
                "allowed_evidence_tiers": [
                    tier.value for tier in (EvidenceTier.T1, EvidenceTier.T2, EvidenceTier.T3)
                ],
                "authority_boundary": (
                    "Chat is a bounded soft prior only; it cannot set legality, price, "
                    "availability, selection, enrollment, or captaincy constraints."
                ),
            },
            "summary": {
                "messages": len(self._messages),
                "claims": len(self._notes),
                "riders": len(riders),
                "source_messages": sum(bool(extract_urls(message.text)) for message in self.messages),
                "evidence_tiers": dict(sorted(tier_counts.items())),
                "lifecycles": dict(sorted(lifecycle_counts.items())),
            },
            "riders": riders,
            "stage_intent": {
                stage: sorted(entries, key=lambda entry: (-abs(entry["signal"]), entry["rider_key"]))
                for stage, entries in sorted(stage_intent.items(), key=lambda item: int(item[0]))
            },
            "speakers": speaker_stats,
        }

    def write_digest(self, path: str | Path) -> dict[str, Any]:
        digest = self.build_digest()
        _atomic_json(Path(path), digest)
        return digest


def signals_by_rider_stage(
    digest: dict[str, Any],
    *,
    stage_max: int = 21,
) -> dict[str, dict[int, float]]:
    """Read normalized stage signals from a schema-v2 digest."""

    if int(digest.get("schema_version", 0)) != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported expert-chat digest schema {digest.get('schema_version')!r}"
        )
    signals: dict[str, dict[int, float]] = {}
    for rider_key, row in digest.get("riders", {}).items():
        stage_values = row.get("stage_signals", {})
        signals[rider_key] = {
            stage: max(-1.0, min(1.0, float(stage_values.get(str(stage), 0.0))))
            for stage in range(1, stage_max + 1)
        }
    return signals


def apply_signal(
    value: float,
    signal: float,
    *,
    max_adjustment: float = CONSUMER_MAX_ADJUSTMENT,
) -> float:
    """Apply the bounded chat prior once to an objective value."""

    if not 0.0 <= max_adjustment <= CONSUMER_MAX_ADJUSTMENT:
        raise ValueError(
            f"max_adjustment must be between 0 and {CONSUMER_MAX_ADJUSTMENT}"
        )
    bounded_signal = max(SIGNAL_MIN, min(SIGNAL_MAX, float(signal)))
    return float(value) * (1.0 + bounded_signal * max_adjustment)
