"""Shared test kit for fulfillment-svc: real Postgres (migrated by Alembic), settings and message helpers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from commerce_common import events
from commerce_common.db import create_engine, create_session_factory
from commerce_common.money import MoneyDTO
from fulfillment_svc.migrate import upgrade
from fulfillment_svc.settings import FulfillmentSettings


def settings(**overrides: Any) -> FulfillmentSettings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://unused/unused",
        "rabbitmq_url": "amqp://guest:guest@localhost:5672/",
        "app_env": "test",
        "log_level": "WARNING",
        "log_format": "console",
        "fulfillment_mock_latency_ms": 0,
        **overrides,
    }
    return FulfillmentSettings(**values)


def order_paid(*skus: str, order_id: str | None = None) -> events.Envelope:
    return events.envelope(
        events.ORDER_PAID,
        "checkout-svc",
        events.OrderPaidV1(
            order_id=order_id or f"ord_{uuid.uuid4().hex[:24]}",
            conversation_id=f"web_{uuid.uuid4().hex}",
            total=MoneyDTO(amount_minor=18900, currency="USD"),
            lines=[events.OrderLine(sku_id=sku, name=sku, quantity=1) for sku in skus or ("sku_a",)],
        ),
    )


async def outbox_rows(sessions: async_sessionmaker[AsyncSession], order_id: str) -> list[dict[str, Any]]:
    async with sessions() as session:
        return list(
            (
                await session.execute(
                    text("SELECT event_type, envelope FROM outbox WHERE envelope->'data'->>'order_id' = :o"),
                    {"o": order_id},
                )
            ).mappings()
        )


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer("pgvector/pgvector:0.8.6-pg18", driver="asyncpg") as pg:
        yield pg


@pytest.fixture(scope="session")
async def fulfillment_db(postgres: PostgresContainer) -> str:
    base = postgres.get_connection_url()
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text("CREATE DATABASE fulfillment"))
    await admin.dispose()
    url = base.rsplit("/", 1)[0] + "/fulfillment"
    await asyncio.to_thread(upgrade, url)
    return url


@pytest.fixture(scope="session")
async def sessions(fulfillment_db: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(fulfillment_db, pool_size=5, max_overflow=0)
    yield create_session_factory(engine)
    await engine.dispose()
