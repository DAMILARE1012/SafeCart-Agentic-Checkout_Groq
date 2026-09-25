"""Versioned event contracts shared by every service (docs §7.2).

Routing key == event type (``order.paid.v1``). Breaking changes get a new version (``.v2``)
published side by side, so consumers migrate on their own schedule.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, Field

from commerce_common.money import MoneyDTO

ORDER_PAID = "order.paid.v1"
FULFILLMENT_SUCCEEDED = "fulfillment.succeeded.v1"
FULFILLMENT_FAILED = "fulfillment.failed.v1"


class Envelope(BaseModel):
    """CloudEvents-style envelope carried as the AMQP body."""

    id: str
    type: str
    source: str
    time: datetime
    correlation_id: str | None = None
    data: dict[str, Any]


def envelope(event_type: str, source: str, data: BaseModel) -> Envelope:
    return Envelope(
        id=f"evt_{uuid.uuid4().hex}",
        type=event_type,
        source=source,
        time=datetime.now(UTC),
        correlation_id=structlog.contextvars.get_contextvars().get("request_id"),
        data=data.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------
class OrderLine(BaseModel):
    sku_id: str
    name: str
    quantity: int = Field(ge=1)


class OrderPaidV1(BaseModel):
    order_id: str
    conversation_id: str
    total: MoneyDTO
    lines: list[OrderLine]


class FulfillmentSucceededV1(BaseModel):
    order_id: str
    fulfillment_id: str
    carrier: str
    tracking_number: str


class FulfillmentFailedV1(BaseModel):
    order_id: str
    fulfillment_id: str
    reason: str
