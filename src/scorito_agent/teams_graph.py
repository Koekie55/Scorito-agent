"""Guarded Microsoft Graph delivery to a configured Teams self-chat."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TEAMS_NOTES_CHAT_ID = "48:notes"


@dataclass(frozen=True, slots=True)
class TeamsGraphConfig:
    access_token: str
    self_chat_id: str
    expected_user: str = "Quinten Koekenbier"

    @classmethod
    def from_environment(cls) -> TeamsGraphConfig | None:
        token = os.environ.get("SCORITO_TEAMS_ACCESS_TOKEN", "").strip()
        chat_id = os.environ.get("SCORITO_TEAMS_SELF_CHAT_ID", "").strip()
        expected_user = os.environ.get(
            "SCORITO_TEAMS_EXPECTED_USER", "Quinten Koekenbier"
        ).strip()
        if not token and not chat_id:
            return None
        missing = [
            name
            for name, value in (
                ("SCORITO_TEAMS_ACCESS_TOKEN", token),
                ("SCORITO_TEAMS_SELF_CHAT_ID", chat_id),
                ("SCORITO_TEAMS_EXPECTED_USER", expected_user),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Teams configuration is incomplete: {', '.join(missing)}")
        return cls(token, chat_id, expected_user)


def send_to_teams_self_chat(
    config: TeamsGraphConfig,
    message: str,
    *,
    session: Any = requests,
) -> str:
    """Verify the delegated identity and self-chat membership, then send once."""
    headers = {
        "Authorization": f"Bearer {config.access_token}",
        "Accept": "application/json",
    }
    me_response = session.get(f"{GRAPH_ROOT}/me", headers=headers, timeout=30)
    me_response.raise_for_status()
    me = me_response.json()
    display_name = str(me.get("displayName") or "")
    if display_name.casefold() != config.expected_user.casefold():
        raise RuntimeError(
            f"Graph token belongs to {display_name!r}, expected {config.expected_user!r}"
        )

    encoded_chat_id = quote(config.self_chat_id, safe="")
    if config.self_chat_id.casefold() != TEAMS_NOTES_CHAT_ID:
        chat_response = session.get(
            f"{GRAPH_ROOT}/chats/{encoded_chat_id}",
            headers=headers,
            params={"$expand": "members"},
            timeout=30,
        )
        chat_response.raise_for_status()
        members = chat_response.json().get("members") or []
        member_ids = {str(member.get("userId") or "") for member in members}
        if member_ids != {str(me.get("id") or "")}:
            raise RuntimeError("configured Teams chat is not the authenticated user's self-chat")

    send_response = session.post(
        f"{GRAPH_ROOT}/chats/{encoded_chat_id}/messages",
        headers={**headers, "Content-Type": "application/json"},
        json={"body": {"contentType": "text", "content": message}},
        timeout=30,
    )
    send_response.raise_for_status()
    message_id = str(send_response.json().get("id") or "")
    if not message_id:
        raise RuntimeError("Microsoft Graph accepted the request without a message ID")
    return message_id
