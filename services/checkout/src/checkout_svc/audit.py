"""Hash-chained audit log (docs §9, P7).

Each row stores ``hash = sha256(prev_hash + canonical(row))``. Writers take a transaction-scoped
advisory lock so the chain is strictly linear; ``verify_chain`` recomputes it end to end.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from checkout_svc.models import AuditLog

GENESIS = "0" * 64
_CHAIN_LOCK = 0x61756469  # arbitrary constant: 'audi'


def _digest(prev_hash: str, fields: dict[str, Any]) -> str:
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{prev_hash}|{canonical}".encode()).hexdigest()


def _fields(row: AuditLog) -> dict[str, Any]:
    return {
        "actor_type": row.actor_type,
        "actor_id": row.actor_id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "before": row.before,
        "after": row.after,
        "correlation_id": row.correlation_id,
    }


async def record(
    session: AsyncSession,
    *,
    actor_type: str,
    actor_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit row inside the caller's transaction (commits or rolls back with it)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CHAIN_LOCK})
    prev = (await session.execute(select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1))).scalar()
    row = AuditLog(
        actor_type=actor_type,
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before,
        after=after,
        correlation_id=structlog.contextvars.get_contextvars().get("request_id"),
        prev_hash=prev or GENESIS,
    )
    row.hash = _digest(row.prev_hash, _fields(row))
    session.add(row)
    await session.flush()
    return row


async def verify_chain(session: AsyncSession) -> tuple[bool, int | None]:
    """(ok, id of the first broken row)."""
    prev = GENESIS
    for row in (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars():
        if row.prev_hash != prev or row.hash != _digest(prev, _fields(row)):
            return False, row.id
        prev = row.hash
    return True, None
