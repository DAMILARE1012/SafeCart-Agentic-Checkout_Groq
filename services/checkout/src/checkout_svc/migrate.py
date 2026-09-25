"""One-shot migration job (compose: checkout-migrate). Runs before any API replica starts."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from commerce_common.observability import configure_logging
from commerce_common.settings import ServiceSettings

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class MigrationSettings(ServiceSettings):
    """Least privilege: the migration job needs the database URL and nothing else (no keys, no secrets)."""

    otel_service_name: str = "checkout-migrate"
    database_url: str


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    # Passed via attributes (not the ini) so passwords with '%' need no escaping.
    cfg.attributes["database_url"] = database_url
    return cfg


def upgrade(database_url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def main() -> None:
    settings = MigrationSettings()
    configure_logging(settings)
    upgrade(settings.database_url)
    print("checkout-svc: migrations applied", file=sys.stderr)


if __name__ == "__main__":
    main()
