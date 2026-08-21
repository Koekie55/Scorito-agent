"""Secure SMTP delivery for rider-news digests."""

from __future__ import annotations

import html
import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

from .models import Highlight


def load_env_file(path: Path) -> None:
    """Load missing environment variables without logging values."""
    if not path.exists():
        return
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid environment line {line_number} in {path}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True, slots=True)
class SMTPConfig:
    host: str
    port: int
    sender: str
    recipients: tuple[str, ...]
    username: str | None
    password: str | None
    use_ssl: bool
    starttls: bool

    @classmethod
    def from_environment(cls) -> SMTPConfig:
        host = os.environ.get("SCORITO_NEWS_SMTP_HOST", "").strip()
        sender = os.environ.get("SCORITO_NEWS_EMAIL_FROM", "").strip()
        raw_recipients = os.environ.get("SCORITO_NEWS_EMAIL_TO", "")
        recipients = tuple(
            address.strip()
            for address in raw_recipients.replace(";", ",").split(",")
            if address.strip()
        )
        missing = [
            name
            for name, value in (
                ("SCORITO_NEWS_SMTP_HOST", host),
                ("SCORITO_NEWS_EMAIL_FROM", sender),
                ("SCORITO_NEWS_EMAIL_TO", recipients),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"email requested but configuration is missing: {', '.join(missing)}")
        port = int(os.environ.get("SCORITO_NEWS_SMTP_PORT", "").strip() or "587")
        if not 1 <= port <= 65535:
            raise ValueError("SCORITO_NEWS_SMTP_PORT must be between 1 and 65535")
        username = os.environ.get("SCORITO_NEWS_SMTP_USERNAME", "").strip() or None
        password = os.environ.get("SCORITO_NEWS_SMTP_PASSWORD") or None
        if bool(username) != bool(password):
            raise ValueError("SMTP username and password must either both be set or both be omitted")
        use_ssl = _env_bool("SCORITO_NEWS_SMTP_SSL", port == 465)
        starttls = _env_bool("SCORITO_NEWS_SMTP_STARTTLS", not use_ssl)
        if use_ssl and starttls:
            raise ValueError("SMTP SSL and STARTTLS cannot both be enabled")
        return cls(host, port, sender, recipients, username, password, use_ssl, starttls)


def _plain_digest(race_name: str, generated_at: str, highlights: list[Highlight]) -> str:
    lines = [
        f"{race_name} rider-news highlights",
        f"Generated: {generated_at}",
        "",
        "News is evidence, not a scoring guarantee. Community claims require independent verification.",
        "",
    ]
    for number, highlight in enumerate(highlights, start=1):
        rider_names = ", ".join(rider.name for rider in highlight.riders) or "Race-wide"
        lines.extend(
            (
                f"{number}. {highlight.item.title}",
                f"   Riders: {rider_names}",
                f"   Source: {highlight.item.publisher or highlight.item.source_name} (tier {highlight.item.source_tier})",
                f"   Published: {highlight.item.published_at.isoformat()}",
                f"   Signals: {', '.join(highlight.categories)}; impact={highlight.impact}; verification={highlight.verification}",
                f"   Selection use: {highlight.decision_hint}",
                f"   Evidence: {highlight.evidence}",
                f"   Link: {highlight.item.url}",
                "",
            )
        )
    return "\n".join(lines)


def _html_digest(race_name: str, generated_at: str, highlights: list[Highlight]) -> str:
    rows = []
    for highlight in highlights:
        rider_names = ", ".join(rider.name for rider in highlight.riders) or "Race-wide"
        publisher = highlight.item.publisher or highlight.item.source_name
        rows.append(
            "<article style='margin:0 0 22px'>"
            f"<h3 style='margin:0 0 5px'><a href='{html.escape(highlight.item.url, quote=True)}'>{html.escape(highlight.item.title)}</a></h3>"
            f"<div><strong>Riders:</strong> {html.escape(rider_names)}</div>"
            f"<div><strong>Source:</strong> {html.escape(publisher)} (tier {highlight.item.source_tier})</div>"
            f"<div><strong>Signals:</strong> {html.escape(', '.join(highlight.categories))}; "
            f"impact={html.escape(highlight.impact)}; verification={html.escape(highlight.verification)}</div>"
            f"<div><strong>Selection use:</strong> {html.escape(highlight.decision_hint)}</div>"
            f"<p>{html.escape(highlight.evidence)}</p>"
            "</article>"
        )
    return (
        "<!doctype html><html><body>"
        f"<h2>{html.escape(race_name)} rider-news highlights</h2>"
        f"<p>Generated: {html.escape(generated_at)}</p>"
        "<p><em>News is evidence, not a scoring guarantee. Community claims require independent verification.</em></p>"
        + "".join(rows)
        + "</body></html>"
    )


def send_digest(
    config: SMTPConfig,
    *,
    race_name: str,
    generated_at: str,
    slot_label: str,
    highlights: list[Highlight],
) -> None:
    if not highlights:
        return
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = ", ".join(config.recipients)
    message["Subject"] = f"[Scorito {race_name}] {len(highlights)} new rider-news highlights - {slot_label}"
    message.set_content(_plain_digest(race_name, generated_at, highlights))
    message.add_alternative(_html_digest(race_name, generated_at, highlights), subtype="html")

    context = ssl.create_default_context()
    if config.use_ssl:
        with smtplib.SMTP_SSL(config.host, config.port, timeout=30, context=context) as smtp:
            if config.username:
                smtp.login(config.username, config.password or "")
            smtp.send_message(message)
        return
    with smtplib.SMTP(config.host, config.port, timeout=30) as smtp:
        smtp.ehlo()
        if config.starttls:
            smtp.starttls(context=context)
            smtp.ehlo()
        if config.username:
            smtp.login(config.username, config.password or "")
        smtp.send_message(message)
