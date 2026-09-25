"""Shared test kit for commerce-svc (helpers + fixtures).

Integration fixtures: a real Postgres (same image as compose), migrated + seeded once per session."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer

from commerce_common.auth import hash_api_key
from commerce_common.db import create_engine, create_session_factory
from commerce_svc.main import create_app
from commerce_svc.migrate import upgrade
from commerce_svc.seed import seed
from commerce_svc.settings import CommerceSettings

AGENT_KEY = "agent-test-key"
CHECKOUT_KEY = "checkout-test-key"
POSTGRES_IMAGE = "pgvector/pgvector:0.8.6-pg18"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
async def settings(database_url: str) -> CommerceSettings:
    await asyncio.to_thread(upgrade, database_url)  # alembic runs its own event loop
    engine = create_engine(database_url, pool_size=1, max_overflow=0)
    async with create_session_factory(engine)() as session, session.begin():
        await seed(session)
    await engine.dispose()
    return CommerceSettings(
        database_url=database_url,
        app_env="test",
        log_format="console",
        log_level="WARNING",
        supported_currencies=["USD", "EUR", "GBP", "NGN", "JPY"],
        field_encryption_key=Fernet.generate_key().decode(),
        agent_svc_api_key_hash=hash_api_key(AGENT_KEY),
        checkout_svc_api_key_hash=hash_api_key(CHECKOUT_KEY),
    )


@pytest.fixture(scope="session")
async def client(settings: CommerceSettings) -> AsyncIterator[AsyncClient]:
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://commerce") as http:
        yield http


@pytest.fixture
def agent() -> dict[str, str]:
    return {"Authorization": f"Bearer {AGENT_KEY}"}


@pytest.fixture
def checkout() -> dict[str, str]:
    return {"Authorization": f"Bearer {CHECKOUT_KEY}"}


def key() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}


def conversation() -> str:
    return f"conv_{uuid.uuid4().hex[:12]}"
