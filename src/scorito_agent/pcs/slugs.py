"""Rider slug helpers for ProCyclingStats.

The default rule mirrors the PHP reference implementation in
``jvdlaar/scorito``: ASCII-slugify ``FirstName LastName``, lowercase it, and
replace word separators with hyphens.  PCS has a few rider-specific URL slugs;
``RIDER_SLUG_EXCEPTIONS`` reproduces the reference ``formatRiderName`` switch.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

RIDER_SLUG_EXCEPTIONS: dict[str, str] = {
    "negasi-haylu-abreha": "negasi-abreha",
    "mikkel-frolich-honore": "mikkel-honore",
    "daniel-martin": "dan-martin",
    "omer-goldshtein": "omer-goldstein",
    "chris-froome": "christopher-froome",
    "alexey-lutsenko": "aleksey-lutsenko",
    "soren-kragh": "soren-kragh-andersen",
    "fred-wright": "alfred-wright",
    "magnus-cort": "magnus-cort-nielsen",
    "ivan-garcia": "ivan-garcia-cortina",
    "georg-zimmerman": "georg-zimmermann",
    "brandon-rivera": "brandon-smith-rivera-vargas",
    "einer-rubio": "einer-augusto-rubio-reyes",
    "diego-camargo": "diego-andres-camargo",
}

_TRANSLITERATION = str.maketrans(
    {
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "ð": "d",
        "Ð": "D",
        "þ": "th",
        "Þ": "Th",
        "ł": "l",
        "Ł": "L",
        "ß": "ss",
        "æ": "ae",
        "Æ": "Ae",
        "œ": "oe",
        "Œ": "Oe",
    }
)


def _name_from_parts(first: str | Mapping[str, Any], last: str | None) -> str:
    if isinstance(first, Mapping):
        first_name = str(first.get("FirstName") or first.get("first_name") or first.get("first") or "")
        last_name = str(first.get("LastName") or first.get("last_name") or first.get("last") or "")
        full_name = str(first.get("name") or "")
        return " ".join(part for part in (first_name, last_name) if part).strip() or full_name.strip()
    if last is None:
        return str(first).strip()
    return f"{first} {last}".strip()


def ascii_slug(text: str) -> str:
    """Return a lowercase ASCII slug compatible with Symfony's AsciiSlugger."""

    text = text.translate(_TRANSLITERATION)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text)
    return re.sub(r"-+", "-", text).strip("-").lower()


def slugify_rider(first: str | Mapping[str, Any], last: str | None = None) -> str:
    """Return the PCS rider slug for a name or Scorito-style rider dict."""

    default_slug = ascii_slug(_name_from_parts(first, last))
    return RIDER_SLUG_EXCEPTIONS.get(default_slug, default_slug)


def slug_from_name(name: str) -> str:
    """Alias for callers that already have one display-name string."""

    return slugify_rider(name)
