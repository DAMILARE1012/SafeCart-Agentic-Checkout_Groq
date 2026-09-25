"""One-shot job (compose: agent-migrate): creates/updates the LangGraph checkpoint tables."""

from __future__ import annotations

import asyncio
import sys

from commerce_common.observability import configure_logging
from commerce_common.settings import ServiceSettings


class MigrationSettings(ServiceSettings):
    """Least privilege: only the database URL (no LLM or service keys)."""

    otel_service_name: str = "agent-migrate"
    database_url: str


async def upgrade(database_url: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(database_url) as saver:
        await saver.setup()  # idempotent: applies any new checkpoint-schema migrations


def main() -> None:
    settings = MigrationSettings()
    configure_logging(settings)
    asyncio.run(upgrade(settings.database_url))
    print("agent-svc: checkpoint schema ready", file=sys.stderr)


if __name__ == "__main__":
    main()
