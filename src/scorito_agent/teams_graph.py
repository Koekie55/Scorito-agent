"""Guarded Microsoft Graph delivery to a configured Teams self-chat."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
TEAMS_NOTES_CHAT_ID = "48:notes"
GRAPH_SCOPES = ("User.Read", "Chat.ReadBasic", "ChatMessage.Send")


@dataclass(frozen=True, slots=True)
class TeamsGraphConfig:
    access_token: str | None
    self_chat_id: str
    expected_user_id: str
    expected_upn: str
    expected_user: str = "Quinten Koekenbier"
    client_id: str | None = None
    tenant_id: str | None = None
    token_cache_path: Path | None = None

    @classmethod
    def from_environment(cls) -> TeamsGraphConfig | None:
        token = os.environ.get("SCORITO_TEAMS_ACCESS_TOKEN", "").strip()
        chat_id = os.environ.get("SCORITO_TEAMS_SELF_CHAT_ID", "").strip()
        client_id = os.environ.get("SCORITO_TEAMS_CLIENT_ID", "").strip()
        tenant_id = os.environ.get("SCORITO_TEAMS_TENANT_ID", "").strip()
        expected_user = os.environ.get(
            "SCORITO_TEAMS_EXPECTED_USER", "Quinten Koekenbier"
        ).strip()
        expected_user_id = os.environ.get("SCORITO_TEAMS_EXPECTED_USER_ID", "").strip()
        expected_upn = os.environ.get("SCORITO_TEAMS_EXPECTED_UPN", "").strip()
        cache_value = os.environ.get("SCORITO_TEAMS_TOKEN_CACHE", "").strip()
        cache_path = Path(cache_value) if cache_value else (
            Path(os.environ.get("LOCALAPPDATA", Path.home()))
            / "ScoritoAgent"
            / "teams-token-cache.bin"
        )
        if not token and not client_id and not chat_id:
            return None
        missing = [
            name
            for name, value in (
                ("SCORITO_TEAMS_SELF_CHAT_ID", chat_id),
                ("SCORITO_TEAMS_EXPECTED_USER_ID", expected_user_id),
                ("SCORITO_TEAMS_EXPECTED_UPN", expected_upn),
                ("SCORITO_TEAMS_EXPECTED_USER", expected_user),
            )
            if not value
        ]
        if not token:
            missing.extend(
                name
                for name, value in (
                    ("SCORITO_TEAMS_CLIENT_ID", client_id),
                    ("SCORITO_TEAMS_TENANT_ID", tenant_id),
                )
                if not value
            )
        if missing:
            raise ValueError(f"Teams configuration is incomplete: {', '.join(missing)}")
        return cls(
            token or None,
            chat_id,
            expected_user_id,
            expected_upn,
            expected_user,
            client_id or None,
            tenant_id or None,
            cache_path,
        )


def _public_client(config: TeamsGraphConfig) -> Any:
    if not config.client_id or not config.tenant_id or not config.token_cache_path:
        raise ValueError("durable Teams authentication is not configured")
    from msal import PublicClientApplication
    from msal_extensions import FilePersistenceWithDataProtection, PersistedTokenCache

    config.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
    persistence = FilePersistenceWithDataProtection(str(config.token_cache_path))
    cache = PersistedTokenCache(persistence)
    return PublicClientApplication(
        config.client_id,
        authority=f"https://login.microsoftonline.com/{config.tenant_id}",
        token_cache=cache,
    )


def acquire_access_token(config: TeamsGraphConfig) -> str:
    if config.access_token:
        return config.access_token
    app = _public_client(config)
    accounts = app.get_accounts()
    matching_accounts = [
        account
        for account in accounts
        if (
            str(account.get("local_account_id") or "").casefold()
            == config.expected_user_id.casefold()
            and str(account.get("username") or "").casefold()
            == config.expected_upn.casefold()
        )
    ]
    if len(matching_accounts) != 1:
        raise RuntimeError(
            "cached Teams login does not uniquely match the configured Entra user ID and UPN"
        )
    result = app.acquire_token_silent(
        list(GRAPH_SCOPES), account=matching_accounts[0]
    )
    token = str((result or {}).get("access_token") or "")
    if not token:
        raise RuntimeError(
            "no cached Teams login is available; run scripts/configure_teams_graph.py"
        )
    return token


def authorize_device_login(
    config: TeamsGraphConfig,
    *,
    prompt: Any = print,
    session: Any = requests,
) -> None:
    """Complete one delegated login and save its refreshable token using Windows DPAPI."""
    app = _public_client(config)
    flow = app.initiate_device_flow(scopes=list(GRAPH_SCOPES))
    if "user_code" not in flow:
        raise RuntimeError(f"could not start Teams device login: {flow.get('error_description')}")
    prompt(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if not result.get("access_token"):
        raise RuntimeError(
            f"Teams device login failed: {result.get('error_description') or result.get('error')}"
        )
    _verified_user_id(config, str(result["access_token"]), session=session)


def _verified_user_id(
    config: TeamsGraphConfig,
    access_token: str,
    *,
    session: Any,
) -> str:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    me_response = session.get(f"{GRAPH_ROOT}/me", headers=headers, timeout=30)
    me_response.raise_for_status()
    me = me_response.json()
    user_id = str(me.get("id") or "")
    upn = str(me.get("userPrincipalName") or "")
    if (
        user_id.casefold() != config.expected_user_id.casefold()
        or upn.casefold() != config.expected_upn.casefold()
    ):
        raise RuntimeError(
            f"Graph token belongs to user ID {user_id!r} / UPN {upn!r}, "
            f"expected {config.expected_user_id!r} / {config.expected_upn!r}"
        )
    return user_id


def send_to_teams_self_chat(
    config: TeamsGraphConfig,
    message: str,
    *,
    session: Any = requests,
) -> str:
    """Verify the delegated identity and self-chat membership, then send once."""
    access_token = acquire_access_token(config)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    user_id = _verified_user_id(config, access_token, session=session)

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
        if member_ids != {user_id}:
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
