"""Reliable messaging on RabbitMQ: transactional outbox (producer) + inbox (consumer) (docs §3.4, §8).

Producer: the event row is inserted in the SAME transaction as the state change (``enqueue``), and a
relay publishes it afterwards with publisher confirms (``relay``). A crash can delay an event, but
can never lose it or emit one for a change that rolled back.

Consumer: ``claim`` inserts the message id into the inbox inside the handler's transaction. A
redelivered message finds its id and is skipped, so at-least-once delivery becomes effectively-once
processing. If the handler fails, the transaction (claim included) rolls back and the message is
nacked for redelivery. Quorum queues cap redeliveries, then dead-letter the message for a human.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aio_pika
import structlog
from faststream.rabbit import ExchangeType, QueueType, RabbitBroker, RabbitExchange, RabbitQueue
from faststream.rabbit.schemas.queue import QuorumQueueArgs
from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from commerce_common.events import Envelope

log = structlog.get_logger("messaging")


# ---------------------------------------------------------------------------
# Tables (each service adds them to its own metadata/database)
# ---------------------------------------------------------------------------
def outbox_table(metadata: MetaData) -> Table:
    return Table(
        "outbox",
        metadata,
        Column("id", String(40), primary_key=True),  # = envelope id = AMQP message_id
        Column("event_type", String(80), nullable=False),
        Column("envelope", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        Column("published_at", DateTime(timezone=True)),
        Column("attempts", Integer, nullable=False, server_default="0"),
        Column("last_error", Text),
        Index("ix_outbox_unpublished", "created_at", postgresql_where="published_at IS NULL"),
    )


def inbox_table(metadata: MetaData) -> Table:
    return Table(
        "inbox",
        metadata,
        Column("message_id", String(64), primary_key=True),
        Column("event_type", String(80), nullable=False),
        Column("processed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


# ---------------------------------------------------------------------------
# Producer side
# ---------------------------------------------------------------------------
async def enqueue(session: AsyncSession, outbox: Table, event: Envelope) -> None:
    """Stage an event inside the caller's transaction (commits or rolls back with the state change)."""
    await session.execute(
        insert(outbox).values(id=event.id, event_type=event.type, envelope=event.model_dump(mode="json"))
    )


async def relay(
    sessions: async_sessionmaker[AsyncSession],
    outbox: Table,
    broker: RabbitBroker,
    exchange: RabbitExchange,
    *,
    batch: int = 50,
) -> int:
    """Publish pending outbox rows (oldest first). Safe on several replicas: rows are row-locked with
    SKIP LOCKED while being published, and marked published only after the broker confirms."""
    published = 0
    async with sessions() as session, session.begin():
        rows = (
            await session.execute(
                select(outbox.c.id, outbox.c.event_type, outbox.c.envelope)
                .where(outbox.c.published_at.is_(None))
                .order_by(outbox.c.created_at)
                .limit(batch)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            try:
                await broker.publish(
                    row.envelope,
                    exchange=exchange,
                    routing_key=row.event_type,
                    message_id=row.id,
                    message_type=row.event_type,
                    correlation_id=row.envelope.get("correlation_id"),
                    persist=True,  # survives a broker restart
                )
            except Exception as exc:
                await session.execute(
                    outbox.update()
                    .where(outbox.c.id == row.id)
                    .values(attempts=outbox.c.attempts + 1, last_error=f"{type(exc).__name__}: {exc}"[:2000])
                )
                log.warning("outbox_publish_failed", event_id=row.id, error=type(exc).__name__)
                break  # keep order: retry this row first on the next tick
            await session.execute(
                outbox.update().where(outbox.c.id == row.id).values(published_at=datetime.now(UTC))
            )
            published += 1
    if published:
        log.info("outbox_published", count=published)
    return published


# ---------------------------------------------------------------------------
# Consumer side
# ---------------------------------------------------------------------------
async def claim(session: AsyncSession, inbox: Table, message_id: str, event_type: str) -> bool:
    """True the first time a message id is seen (inside the handler's transaction), False for duplicates."""
    inserted = (
        await session.execute(
            insert(inbox)
            .values(message_id=message_id, event_type=event_type)
            .on_conflict_do_nothing(index_elements=[inbox.c.message_id])
            .returning(inbox.c.message_id)
        )
    ).first()
    return inserted is not None


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------
def events_exchange(name: str) -> RabbitExchange:
    return RabbitExchange(name, type=ExchangeType.TOPIC, durable=True)


def consumer_queue(
    name: str, *, routing_key: str, dead_letter_exchange: str, max_deliveries: int
) -> RabbitQueue:
    """Durable quorum queue. After ``max_deliveries`` failed attempts the message is dead-lettered."""
    arguments: QuorumQueueArgs = {
        "x-dead-letter-exchange": dead_letter_exchange,
        "x-delivery-limit": max_deliveries,
    }
    return RabbitQueue(
        name,
        queue_type=QueueType.QUORUM,
        durable=True,
        routing_key=routing_key,
        arguments=arguments,
    )


async def declare_dead_letter_queue(broker: RabbitBroker, dead_letter_exchange: str) -> None:
    """Messages that exhausted their deliveries land in ``<dlx>.q`` for inspection (and alerting)."""
    exchange = await broker.declare_exchange(
        RabbitExchange(dead_letter_exchange, type=ExchangeType.FANOUT, durable=True)
    )
    queue = await broker.declare_queue(
        RabbitQueue(f"{dead_letter_exchange}.q", queue_type=QueueType.QUORUM, durable=True)
    )
    await queue.bind(exchange)


async def queue_depth(url: str, name: str) -> int:
    """Messages waiting in ``name`` (used to alert on a growing dead-letter queue). Declared passively on a
    short-lived connection, so the count is live and nothing is created if the queue doesn't exist."""
    connection = await aio_pika.connect(url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(name, passive=True)
        return int(queue.declaration_result.message_count or 0)
