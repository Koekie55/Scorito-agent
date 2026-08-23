import pytest

from scorito_agent.teams_graph import TeamsGraphConfig, send_to_teams_self_chat


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
            return _Response({"id": "user-1", "displayName": "Quinten Koekenbier"})
        return _Response({"members": self.chat_members})

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _Response({"id": "message-1"})


def test_teams_send_verifies_identity_and_self_chat() -> None:
    session = _Session()
    config = TeamsGraphConfig("secret", "chat/id")

    message_id = send_to_teams_self_chat(config, "Stage score", session=session)

    assert message_id == "message-1"
    assert session.posts[0][1]["json"]["body"]["content"] == "Stage score"
    assert "chat%2Fid" in session.posts[0][0]


def test_teams_notes_send_uses_identity_guard_without_chat_lookup() -> None:
    session = _Session()

    message_id = send_to_teams_self_chat(
        TeamsGraphConfig("secret", "48:notes"), "Stage score", session=session
    )

    assert message_id == "message-1"
    assert len(session.gets) == 1
    assert "48%3Anotes" in session.posts[0][0]


def test_teams_send_rejects_a_non_self_chat() -> None:
    session = _Session(chat_members=[{"userId": "user-1"}, {"userId": "user-2"}])

    with pytest.raises(RuntimeError, match="not the authenticated user's self-chat"):
        send_to_teams_self_chat(
            TeamsGraphConfig("secret", "group-chat"),
            "Stage score",
            session=session,
        )

    assert session.posts == []


def test_empty_teams_environment_disables_delivery(monkeypatch) -> None:
    monkeypatch.delenv("SCORITO_TEAMS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("SCORITO_TEAMS_SELF_CHAT_ID", raising=False)

    assert TeamsGraphConfig.from_environment() is None
