"""Typed contracts for evidence-aware expert-chat intelligence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable


class EvidenceTier(StrEnum):
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"
    T4 = "T4"
    HUMOUR = "H"


EVIDENCE_WEIGHTS: dict[EvidenceTier, float] = {
    EvidenceTier.T1: 1.0,
    EvidenceTier.T2: 0.72,
    EvidenceTier.T3: 0.38,
    EvidenceTier.T4: 0.0,
    EvidenceTier.HUMOUR: 0.0,
}


class ClaimLifecycle(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    UNRESOLVED = "unresolved"
    CONTRADICTORY = "contradictory"
    STALE = "stale"


class ClaimAction(StrEnum):
    ASSERT = "assert"
    CORRECT = "correct"
    CLARIFY = "clarify"
    RETRACT = "retract"


class ClaimCategory(StrEnum):
    AVAILABILITY = "availability"
    HEALTH = "health"
    TEAM_ROLE = "team_role"
    STAGE_INTENT = "stage_intent"
    PRICE = "price"
    RESULT = "result"
    FORM = "form"
    TACTICS = "tactics"
    VALUE = "value"
    PREFERENCE = "preference"
    SOURCE_REFERENCE = "source_reference"
    HUMOUR = "humour"
    OTHER = "other"


LIFECYCLE_EXPIRY_DAYS: dict[ClaimCategory, int] = {
    ClaimCategory.AVAILABILITY: 14,
    ClaimCategory.HEALTH: 14,
    ClaimCategory.STAGE_INTENT: 21,
    ClaimCategory.TACTICS: 21,
    ClaimCategory.PRICE: 30,
    ClaimCategory.VALUE: 30,
    ClaimCategory.FORM: 30,
    ClaimCategory.TEAM_ROLE: 60,
    ClaimCategory.PREFERENCE: 60,
    ClaimCategory.RESULT: 365,
    ClaimCategory.SOURCE_REFERENCE: 365,
    ClaimCategory.HUMOUR: 1,
    ClaimCategory.OTHER: 14,
}


@dataclass(frozen=True, slots=True)
class SpeakerProfile:
    """Interpretive calibration metadata, never a truth designation."""

    profile_id: str
    display_name: str
    aliases: tuple[str, ...]
    calibration: float
    strengths: tuple[str, ...]
    caveats: tuple[str, ...]

    @property
    def label(self) -> str:
        return self.display_name

    @property
    def t3_factor(self) -> float:
        return self.calibration

    @property
    def oversized_list_prone(self) -> bool:
        return self.profile_id in {"hemmo", "smiley"}

    @property
    def cautions(self) -> tuple[str, ...]:
        return self.caveats

    def factor_for(self, tier: EvidenceTier) -> float:
        """Calibrate interpretation only; speakers cannot elevate evidence."""

        if tier in {EvidenceTier.T1, EvidenceTier.T2}:
            return 1.0
        if tier == EvidenceTier.T3:
            return self.t3_factor
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "calibration": self.calibration,
            "t3_factor": self.t3_factor,
            "oversized_list_prone": self.oversized_list_prone,
            "strengths": list(self.strengths),
            "caveats": list(self.caveats),
        }


_SPEAKER_PROFILES = (
    SpeakerProfile(
        profile_id="hemmo",
        display_name="Hemmo",
        aliases=("hemmo",),
        calibration=0.78,
        strengths=(
            "budget discipline",
            "roster redundancy",
            "sprint-plus evaluation",
            "contrarian scenarios",
        ),
        caveats=(
            "mixes serious analysis with hype and jokes",
            "often recommends more candidates than a legal squad can contain",
        ),
    ),
    SpeakerProfile(
        profile_id="smiley",
        display_name=":)",
        aliases=(":)",),
        calibration=0.76,
        strengths=(
            "price and results research",
            "roster architecture",
            "team-role research",
            "cheap-rider scouting",
        ),
        caveats=(
            "high candidate volume creates FOMO",
            "long lists are ideas rather than a closed legal roster",
        ),
    ),
    SpeakerProfile(
        profile_id="tom-zwetsloot",
        display_name="Tom Zwetsloot",
        aliases=("tom zwetsloot",),
        calibration=0.65,
        strengths=("race-scenario discussion",),
        caveats=("claims still require source-level evidence",),
    ),
    SpeakerProfile(
        profile_id="zoer",
        display_name="Zoer",
        aliases=("zoer",),
        calibration=0.62,
        strengths=("candidate generation",),
        caveats=("claims still require source-level evidence",),
    ),
    SpeakerProfile(
        profile_id="fleur",
        display_name="Fleur",
        aliases=("fleur",),
        calibration=0.62,
        strengths=("candidate generation",),
        caveats=("claims still require source-level evidence",),
    ),
    SpeakerProfile(
        profile_id="emma",
        display_name="Emma",
        aliases=("emma",),
        calibration=0.62,
        strengths=("candidate generation",),
        caveats=("claims still require source-level evidence",),
    ),
)

COMMUNITY_PROFILE = SpeakerProfile(
    profile_id="community",
    display_name="Community contributor",
    aliases=(),
    calibration=0.55,
    strengths=("candidate generation", "community observations"),
    caveats=("unverified claims require independent evidence",),
)


def normalise_author(author: str) -> str:
    text = str(author or "").strip().lower()
    text = re.sub(r"[^a-z0-9:)\s-]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def speaker_profile(author: str) -> SpeakerProfile:
    key = normalise_author(author)
    if not key:
        return COMMUNITY_PROFILE
    for profile in _SPEAKER_PROFILES:
        if key in profile.aliases:
            return profile
    return COMMUNITY_PROFILE


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TIMESTAMP_FORMATS = (
    "%d/%m/%Y, %H:%M:%S",
    "%d/%m/%Y, %H:%M",
    "%d-%m-%Y, %H:%M:%S",
    "%d-%m-%Y, %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%d.%m.%Y, %H:%M:%S",
    "%d.%m.%Y, %H:%M",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%H:%M:%S",
    "%H:%M",
)


def _try_parse_timestamp(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    text = str(value or "").strip().strip("[]").replace("\u200e", "")
    if not text:
        return None
    iso_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        parsed = None
    if parsed is not None:
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    for timestamp_format in _TIMESTAMP_FORMATS:
        try:
            parsed = datetime.strptime(text, timestamp_format)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC)
    return None


def parse_timestamp(value: datetime | str | None) -> datetime:
    """Parse exported timestamps into deterministic, timezone-aware UTC values."""

    return _try_parse_timestamp(value) or _EPOCH


def message_id(
    author: str,
    text: str,
    sent_at: datetime | str | None = None,
    *,
    occurrence: int = 1,
) -> str:
    if occurrence < 1:
        raise ValueError("occurrence must be at least 1")
    normalised = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    parsed = _try_parse_timestamp(sent_at)
    timestamp_key = parsed.isoformat() if parsed is not None else str(sent_at or "").strip()
    identity = f"{normalise_author(author)}\0{timestamp_key}\0{normalised}"
    if occurrence > 1:
        identity = f"{identity}\0occurrence:{occurrence}"
    payload = identity.encode("utf-8")
    return hashlib.sha1(payload).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    message_id: str
    author_key: str
    author_display: str
    timestamp: datetime
    line_number: int
    text: str
    first_seen_export: int = 1

    @property
    def sent_at(self) -> str:
        """Compatibility alias for schema-v1 callers."""

        return self.timestamp.isoformat()

    @property
    def author(self) -> str:
        return self.author_display

    @property
    def speaker(self) -> SpeakerProfile:
        return speaker_profile(self.author_key)

    @classmethod
    def create(
        cls,
        author: str,
        text: str,
        sent_at: datetime | str | None = None,
        export_index: int = 1,
        *,
        line_number: int = 0,
        occurrence: int = 1,
    ) -> "ChatMessage":
        display = str(author).strip()
        return cls(
            message_id=message_id(
                display,
                text,
                sent_at,
                occurrence=occurrence,
            ),
            author_key=normalise_author(display),
            author_display=display,
            timestamp=parse_timestamp(sent_at),
            line_number=max(0, int(line_number)),
            text=str(text).strip(),
            first_seen_export=export_index,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "author_key": self.author_key,
            "author_display": self.author_display,
            "timestamp": self.timestamp.isoformat(),
            "line_number": self.line_number,
            "text": self.text,
            "first_seen_export": self.first_seen_export,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ChatMessage":
        author_display = str(
            value.get("author_display", value.get("author", ""))
        ).strip()
        timestamp = value.get("timestamp", value.get("sent_at"))
        text = str(value.get("text", ""))
        return cls(
            message_id=str(
                value.get(
                    "message_id",
                    value.get("id", message_id(author_display, text, timestamp)),
                )
            ),
            author_key=str(
                value.get("author_key", normalise_author(author_display))
            ),
            author_display=author_display,
            timestamp=parse_timestamp(timestamp),
            line_number=max(
                0,
                int(value.get("line_number", value.get("source_line", 0)) or 0),
            ),
            text=text,
            first_seen_export=int(value.get("first_seen_export", 1)),
        )


@dataclass(frozen=True, slots=True)
class RiderNote:
    note_id: str
    message_id: str
    rider_key: str
    rider_name: str
    author_key: str
    author_display: str
    timestamp: datetime
    source_line: int
    text: str
    category: ClaimCategory
    action: ClaimAction
    evidence_tier: EvidenceTier
    lifecycle: ClaimLifecycle
    sentiment: float
    extraction_confidence: float
    speaker_factor: float
    list_discount: float
    rider_count_in_message: int
    stages: tuple[int, ...]
    sources: tuple[str, ...]
    source_message_ids: tuple[str, ...]
    superseded_by: str | None = None
    conflicts_with: tuple[str, ...] = ()

    @property
    def sent_at(self) -> str:
        """Compatibility alias for schema-v1 callers."""

        return self.timestamp.isoformat()

    @property
    def speaker_calibration(self) -> float:
        return self.speaker_factor

    @property
    def effective_weight(self) -> float:
        weight = (
            EVIDENCE_WEIGHTS[self.evidence_tier]
            * self.extraction_confidence
            * self.speaker_factor
            * self.list_discount
        )
        return max(0.0, min(1.0, weight))

    @property
    def contributes_to_model(self) -> bool:
        return (
            self.lifecycle == ClaimLifecycle.ACTIVE
            and self.action in {ClaimAction.ASSERT, ClaimAction.CORRECT, ClaimAction.CLARIFY}
            and self.evidence_tier
            in {EvidenceTier.T1, EvidenceTier.T2, EvidenceTier.T3}
            and self.effective_weight > 0
            and self.sentiment != 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "message_id": self.message_id,
            "rider_key": self.rider_key,
            "rider_name": self.rider_name,
            "author_key": self.author_key,
            "author_display": self.author_display,
            "timestamp": self.timestamp.isoformat(),
            "source_line": self.source_line,
            "text": self.text,
            "category": self.category.value,
            "action": self.action.value,
            "evidence_tier": self.evidence_tier.value,
            "lifecycle": self.lifecycle.value,
            "sentiment": round(self.sentiment, 6),
            "extraction_confidence": round(self.extraction_confidence, 6),
            "speaker_factor": round(self.speaker_factor, 6),
            "list_discount": round(self.list_discount, 6),
            "rider_count_in_message": self.rider_count_in_message,
            "stages": list(self.stages),
            "sources": list(self.sources),
            "source_message_ids": list(self.source_message_ids),
            "effective_weight": round(self.effective_weight, 6),
            "contributes_to_model": self.contributes_to_model,
            "superseded_by": self.superseded_by,
            "conflicts_with": list(self.conflicts_with),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RiderNote":
        evidence_tier = EvidenceTier(value.get("evidence_tier", EvidenceTier.T4))
        return cls(
            note_id=str(value["note_id"]),
            message_id=str(value["message_id"]),
            rider_key=str(value["rider_key"]),
            rider_name=str(value["rider_name"]),
            author_key=str(value["author_key"]),
            author_display=str(value.get("author_display", value["author_key"])),
            timestamp=parse_timestamp(value.get("timestamp", value.get("sent_at"))),
            source_line=max(0, int(value.get("source_line", 0) or 0)),
            text=str(value.get("text", "")),
            category=ClaimCategory(value.get("category", ClaimCategory.OTHER)),
            action=ClaimAction(value.get("action", ClaimAction.ASSERT)),
            evidence_tier=evidence_tier,
            lifecycle=ClaimLifecycle(value.get("lifecycle", ClaimLifecycle.ACTIVE)),
            sentiment=float(value.get("sentiment", 0.0)),
            extraction_confidence=float(value.get("extraction_confidence", 0.0)),
            speaker_factor=float(
                value.get(
                    "speaker_factor",
                    value.get("speaker_calibration", 1.0),
                )
            ),
            list_discount=float(value.get("list_discount", 1.0)),
            rider_count_in_message=max(
                1,
                int(value.get("rider_count_in_message", 1) or 1),
            ),
            stages=tuple(int(stage) for stage in value.get("stages", ())),
            sources=tuple(str(source) for source in value.get("sources", ())),
            source_message_ids=tuple(
                str(message_id_value)
                for message_id_value in value.get(
                    "source_message_ids",
                    (value["message_id"],) if value.get("sources") else (),
                )
            ),
            superseded_by=(
                str(value["superseded_by"])
                if value.get("superseded_by") is not None
                else None
            ),
            conflicts_with=tuple(
                str(note_id) for note_id in value.get("conflicts_with", ())
            ),
        )


@dataclass(slots=True)
class RiderIntel:
    rider_key: str
    rider_name: str
    facts: list[RiderNote] = field(default_factory=list)
    opinions: list[RiderNote] = field(default_factory=list)

    @property
    def notes(self) -> list[RiderNote]:
        return [*self.facts, *self.opinions]

    @property
    def stage_targets(self) -> list[int]:
        return sorted({stage for note in self.notes for stage in note.stages})

    @property
    def sources(self) -> list[str]:
        return sorted({source for note in self.notes for source in note.sources})

    def add(self, note: RiderNote) -> None:
        if note.evidence_tier in {EvidenceTier.T1, EvidenceTier.T2}:
            self.facts.append(note)
        else:
            self.opinions.append(note)

    def all_notes(self) -> Iterable[RiderNote]:
        yield from self.facts
        yield from self.opinions


def evidence_weight(tier: EvidenceTier | str) -> float:
    try:
        key = tier if isinstance(tier, EvidenceTier) else EvidenceTier(tier)
    except ValueError:
        return 0.0
    return EVIDENCE_WEIGHTS[key]
