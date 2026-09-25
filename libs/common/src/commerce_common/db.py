"""Async SQLAlchemy helpers shared by every service (each service owns its own database)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

# Deterministic constraint names → stable, reviewable Alembic migrations.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def make_metadata() -> MetaData:
    return MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(url: str, *, pool_size: int = 10, max_overflow: int = 5, echo: bool = False) -> AsyncEngine:
    return create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        echo=echo,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def transactional_session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    """One transaction per request: commit on success, roll back on any exception."""
    async with factory() as session, session.begin():
        yield session
