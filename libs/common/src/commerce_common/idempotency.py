"""Idempotent request handling, stored in the service's own database (docs §8).

Algorithm (all inside the request's single transaction):
1. ``INSERT … ON CONFLICT DO NOTHING`` the (caller, key) row.
2. Inserted → run the handler, store its response, commit together with the
   side effects. Atomic: either both exist or neither does.
3. Conflict → a previous request with that key committed (Postgres makes a
   concurrent duplicate WAIT on the uncommitted row, so we never see a half-done
   one): replay its stored response, or 422 if the payload differs.
Only successful responses are stored; a failed request rolls back its key too,
so a retry re-executes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Header
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, delete, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession

from .errors import BadRequest, ValidationFailed

MAX_KEY_LENGTH = 200


def idempotency_table(metadata: MetaData) -> Table:
    return Table(
        "idempotency_keys",
        metadata,
        Column("caller", String(64), primary_key=True),
        Column("key", String(MAX_KEY_LENGTH), primary_key=True),
        Column("fingerprint", String(64), nullable=False),
        Column("response_status", Integer, nullable=False),
        Column("response_body", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now(), index=True),
    )


def fingerprint(method: str, path: str, body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{method.upper()} {path}\n{canonical}".encode()).hexdigest()


async def idempotency_key_header(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    if not idempotency_key:
        raise BadRequest("idempotency_key_required", "This endpoint requires an Idempotency-Key header")
    if len(idempotency_key) > MAX_KEY_LENGTH:
        raise BadRequest("idempotency_key_invalid", f"Idempotency-Key must be ≤ {MAX_KEY_LENGTH} chars")
    return idempotency_key


async def run_idempotent(
    session: AsyncSession,
    table: Table,
    *,
    caller: str,
    key: str,
    request_fingerprint: str,
    handler: Callable[[], Awaitable[tuple[int, Any]]],
) -> tuple[int, Any]:
    """Runs ``handler`` at most once per (caller, key). Must be called inside a transaction."""
    placeholder = insert(table).values(
        caller=caller, key=key, fingerprint=request_fingerprint, response_status=0, response_body={}
    )
    inserted = (await session.execute(placeholder.on_conflict_do_nothing().returning(table.c.key))).first()

    if inserted is None:
        row = (
            await session.execute(
                select(table.c.fingerprint, table.c.response_status, table.c.response_body).where(
                    table.c.caller == caller, table.c.key == key
                )
            )
        ).one()
        if row.fingerprint != request_fingerprint:
            raise ValidationFailed(
                "idempotency_key_reused", "This Idempotency-Key was already used with a different request"
            )
        return row.response_status, row.response_body

    status, body = await handler()
    await session.execute(
        table.update()
        .where(table.c.caller == caller, table.c.key == key)
        .values(response_status=status, response_body=body)
    )
    return status, body


async def purge_expired(session: AsyncSession, table: Table, *, older_than_hours: int) -> int:
    result = await session.execute(
        delete(table).where(
            table.c.created_at < func.now() - func.make_interval(0, 0, 0, 0, older_than_hours)
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
