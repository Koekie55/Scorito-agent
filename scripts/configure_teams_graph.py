"""Perform the one-time delegated login used by scheduled Teams delivery."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scorito_agent.news.mailer import load_env_file  # noqa: E402
from scorito_agent.teams_graph import (  # noqa: E402
    TeamsGraphConfig,
    authorize_device_login,
    send_to_teams_self_chat,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--send-test", action="store_true")
    args = parser.parse_args(argv)

    load_env_file(args.env_file)
    config = TeamsGraphConfig.from_environment()
    if config is None or not config.client_id or not config.tenant_id:
        parser.error(
            "set SCORITO_TEAMS_CLIENT_ID, SCORITO_TEAMS_TENANT_ID and "
            "SCORITO_TEAMS_SELF_CHAT_ID before login"
        )
    authorize_device_login(config)
    print(f"Teams login cached securely for {config.expected_user}.")
    if args.send_test:
        message_id = send_to_teams_self_chat(
            config, "Scorito scheduled Teams delivery is configured."
        )
        print(f"Test message sent: {message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())