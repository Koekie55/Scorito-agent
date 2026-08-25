import pytest

from scorito_agent import teams_graph
from scorito_agent.teams_graph import (
    TeamsGraphConfig,
    authorize_device_login,
    send_to_teams_self_chat,
)


def _config(access_token="secret", chat_id="48:notes"):
    return TeamsGraphConfig(
        access_token,
        chat_id,
        expected_user_id="user-1",
        expected_upn="quinten@example.com",
    )


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, *, chat_members=None):
        self.chat_members = chat_members or [{"userId": "user-1"}]
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url.endswith("/me"):
            return _Response({
                "id": "user-1",
                "userPrincipalName": "quinten@example.com",
                "displayName": "Quinten Koekenbier",
            })
        return _Response({"members": self.chat_members})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({"id": "message-1"})


def test_teams_send_verifies_identity_and_self_chat() -> None:
    session = _Session()
    config = _config(chat_id="chat/id")

    message_id = send_to_teams_self_chat(config, "Stage score", session=session)

    assert message_id == "message-1"
    assert session.posts[0][1]["json"]["body"]["content"] == "Stage score"
    assert "chat%2Fid" in session.posts[0][0]


def test_teams_notes_send_uses_identity_guard_without_chat_lookup() -> None:
    session = _Session()

    message_id = send_to_teams_self_chat(
        _config(), "Stage score", session=session
    )

    assert message_id == "message-1"
    assert len(session.gets) == 1
    assert "48%3Anotes" in session.posts[0][0]


def test_teams_send_rejects_a_non_self_chat() -> None:
    session = _Session(chat_members=[{"userId": "user-1"}, {"userId": "user-2"}])

    with pytest.raises(RuntimeError, match="not the authenticated user's self-chat"):
        send_to_teams_self_chat(
            _config(chat_id="group-chat"),
            "Stage score",
            session=session,
        )

    assert session.posts == []


def test_empty_teams_environment_disables_delivery(monkeypatch) -> None:
    monkeypatch.delenv("SCORITO_TEAMS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SCORITO_TEAMS_SELF_CHAT_ID", raising=False)
    monkeypatch.delenv("SCORITO_TEAMS_CLIENT_ID", raising=False)

    assert TeamsGraphConfig.from_environment() is None


def test_cached_login_configuration_is_loaded(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SCORITO_TEAMS_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SCORITO_TEAMS_CLIENT_ID", "client-id")
    monkeypatch.setenv("SCORITO_TEAMS_TENANT_ID", "tenant-id")
    monkeypatch.setenv("SCORITO_TEAMS_SELF_CHAT_ID", "48:notes")
    monkeypatch.setenv("SCORITO_TEAMS_EXPECTED_USER_ID", "user-1")
    monkeypatch.setenv("SCORITO_TEAMS_EXPECTED_UPN", "quinten@example.com")
    monkeypatch.setenv("SCORITO_TEAMS_TOKEN_CACHE", str(tmp_path / "cache.bin"))

    config = TeamsGraphConfig.from_environment()

    assert config is not None
    assert config.access_token is None
    assert config.client_id == "client-id"
    assert config.expected_user_id == "user-1"
    assert config.expected_upn == "quinten@example.com"
    assert config.token_cache_path == tmp_path / "cache.bin"


def test_send_uses_token_from_durable_cache(monkeypatch) -> None:
    session = _Session()
    config = _config(access_token=None)
    config = TeamsGraphConfig(
        config.access_token,
        config.self_chat_id,
        config.expected_user_id,
        config.expected_upn,
        client_id="client-id",
        tenant_id="tenant-id",
    )
    monkeypatch.setattr(teams_graph, "acquire_access_token", lambda _: "cached-token")

    send_to_teams_self_chat(config, "Stage score", session=session)

    assert session.posts[0][1]["headers"]["Authorization"] == "Bearer cached-token"


def test_cached_login_selects_matching_identity_not_first_account(monkeypatch) -> None:
    class _App:
        selected_account = None

        def get_accounts(self):
            return [
                {"local_account_id": "other", "username": "other@example.com"},
                {"local_account_id": "user-1", "username": "quinten@example.com"},
            ]

        def acquire_token_silent(self, scopes, *, account):
            self.selected_account = account
            return {"access_token": "matched-token"}

    app = _App()
    monkeypatch.setattr(teams_graph, "_public_client", lambda _: app)

    assert teams_graph.acquire_access_token(_config(access_token=None)) == "matched-token"
    assert app.selected_account["local_account_id"] == "user-1"


def test_send_rejects_same_display_name_with_different_object_id() -> None:
    class _WrongIdentitySession(_Session):
        def get(self, url, **kwargs):
            self.gets.append((url, kwargs))
            return _Response({
                "id": "user-2",
                "userPrincipalName": "other@example.com",
                "displayName": "Quinten Koekenbier",
            })

    session = _WrongIdentitySession()

    with pytest.raises(RuntimeError, match="Graph token belongs to user ID"):
        send_to_teams_self_chat(_config(), "Stage score", session=session)

    assert session.posts == []


def test_device_login_rejects_wrong_identity(monkeypatch) -> None:
    class _App:
        def initiate_device_flow(self, *, scopes):
            return {"user_code": "code", "message": "Sign in"}

        def acquire_token_by_device_flow(self, flow):
            return {"access_token": "wrong-user-token"}

    class _WrongIdentitySession(_Session):
        def get(self, url, **kwargs):
            return _Response({
                "id": "user-2",
                "userPrincipalName": "other@example.com",
                "displayName": "Quinten Koekenbier",
            })

    monkeypatch.setattr(teams_graph, "_public_client", lambda _: _App())

    with pytest.raises(RuntimeError, match="Graph token belongs to user ID"):
        authorize_device_login(
            _config(access_token=None),
            prompt=lambda _message: None,
            session=_WrongIdentitySession(),
        )
