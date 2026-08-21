"""Tolerant parser for Teams / WhatsApp style chat exports.

Exports arrive as pasted text and the layout differs per client, so the parser
recognises several author-line shapes and treats everything else as a
continuation of the message in progress. Anything it cannot attribute is kept
under an ``unknown`` author rather than dropped.
"""

from __future__ import annotations

import re

from .models import ChatMessage, normalise_author

_BRACKET = re.compile(
    r"^\[(?P<ts>[^\]]{3,80})\]\s*"
    r"(?P<author>[^\r\n]{1,200}?):(?:[ \t]+(?P<text>.*))?$"
)
_BRACKET_COMPACT = re.compile(
    r"^\[(?P<ts>[^\]]{3,80})\]\s*"
    r"(?P<author>[^:\r\n]{0,199}[^\s:\r\n]):(?P<text>\S.*)$"
)
_AUTHOR_TIME = re.compile(
    r"^(?P<author>.{1,200}?)[\s,(\[]+"
    r"(?P<ts>\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?)[)\]]?\s*:\s*(?P<text>.*)$"
)
_SIMPLE = re.compile(r"^(?P<author>[^\r\n]{1,40}?):[ \t]+(?P<text>.+)$")
_SIMPLE_COMPACT = re.compile(
    r"^(?P<author>[^:\r\n]{0,39}[^\s:\r\n]):(?P<text>.+)$"
)
_TIME_ONLY = re.compile(
    r"^(?:[A-Za-z]{2,12}\s+)?(?:\d{1,2}[-/.]\d{1,2}(?:[-/.]\d{2,4})?\s+)?"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?$"
)
_DATE_ONLY = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")

# Client chrome that carries no content.
_NOISE = {
    "",
    "edited",
    "bewerkt",
    "reply",
    "reageren",
    "beantwoorden",
    "today",
    "vandaag",
    "yesterday",
    "gisteren",
    "forwarded",
    "doorgestuurd",
    "unread",
    "ongelezen",
    "you",
    "jij",
    "seen",
    "gezien",
    "delivered",
    "afgeleverd",
    "this message was deleted",
    "dit bericht is verwijderd",
}
_NOISE_PREFIXES = ("<attachment", "image ", "afbeelding ")
_BIDI_PREFIX_MARKS = "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff"


def _is_noise(line: str) -> bool:
    lowered = line.strip().lower()
    if lowered in _NOISE:
        return True
    if _DATE_ONLY.match(lowered):
        return True
    return any(lowered.startswith(prefix) for prefix in _NOISE_PREFIXES)


def _looks_like_author(candidate: str) -> bool:
    """A short, punctuation-free label — not a sentence."""
    text = candidate.strip()
    if not text or len(text) > 40:
        return False
    if text.endswith((".", "!", "?", ",")):
        return False
    words = text.split()
    if len(words) > 4:
        return False
    return any(char.isalpha() for char in text) or normalise_author(text) in {":)", ":-)"}


def _clean_line(raw: str) -> str:
    return raw.strip().lstrip(f"{_BIDI_PREFIX_MARKS} \t")


def _clean_author(raw: str) -> str:
    return raw.strip().lstrip(f"{_BIDI_PREFIX_MARKS} \t")


def _structured_header(line: str) -> re.Match[str] | None:
    return _BRACKET.match(line) or _BRACKET_COMPACT.match(line) or _AUTHOR_TIME.match(line)


def _is_structured_export(lines: list[str]) -> bool:
    if any(_structured_header(line) for line in lines):
        return True
    return any(
        _looks_like_author(line)
        and index + 1 < len(lines)
        and bool(_TIME_ONLY.match(lines[index + 1]))
        for index, line in enumerate(lines)
    )


def parse_export(text: str, export_index: int = 1) -> list[ChatMessage]:
    """Parse an exported chat into ordered :class:`ChatMessage` objects."""
    raw_lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [_clean_line(line) for line in raw_lines]
    structured_export = _is_structured_export(lines)
    messages: list[ChatMessage] = []
    message_occurrences: dict[str, int] = {}
    author = "unknown"
    sent_at: str | None = None
    source_line = 0
    buffer: list[str] = []
    message_started = False
    # True right after an author/timestamp header: the next line is body text,
    # never a new "Name: text" header.
    expect_body = False

    def flush() -> None:
        nonlocal buffer, message_started
        body = "\n".join(buffer).strip()
        buffer = []
        if message_started or body:
            message = ChatMessage.create(
                author,
                body,
                sent_at,
                export_index,
                line_number=source_line,
            )
            occurrence = message_occurrences.get(message.message_id, 0) + 1
            message_occurrences[message.message_id] = occurrence
            if occurrence > 1:
                message = ChatMessage.create(
                    author,
                    body,
                    sent_at,
                    export_index,
                    line_number=source_line,
                    occurrence=occurrence,
                )
            messages.append(message)
        message_started = False

    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if _is_noise(line):
            continue

        match = _structured_header(line)
        if match and (structured_export or _looks_like_author(match.group("author"))):
            flush()
            author = _clean_author(match.group("author"))
            sent_at = match.group("ts").strip()
            source_line = index
            inline_text = (match.group("text") or "").strip()
            if inline_text:
                buffer.append(inline_text)
            message_started = True
            expect_body = False
            continue

        # Teams web copy: author on its own line, timestamp on the next one.
        if (
            _looks_like_author(line)
            and index < len(lines)
            and _TIME_ONLY.match(lines[index].strip())
        ):
            flush()
            author = line
            sent_at = lines[index].strip()
            source_line = index
            index += 1
            message_started = True
            expect_body = True
            continue

        if _TIME_ONLY.match(line):
            sent_at = line
            continue

        simple = (
            None
            if structured_export or expect_body
            else (_SIMPLE.match(line) or _SIMPLE_COMPACT.match(line))
        )
        if simple and _looks_like_author(simple.group("author")):
            flush()
            author = simple.group("author").strip()
            source_line = index
            buffer.append(simple.group("text").strip())
            message_started = True
            continue

        if not message_started:
            author = "unknown"
            source_line = index
            message_started = True
        buffer.append(line)
        expect_body = False

    flush()
    return messages
