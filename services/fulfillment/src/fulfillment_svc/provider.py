"""Fulfilment provider boundary (a 3PL / warehouse API). Mock implementation for dev and demos."""

from __future__ import annotations

import asyncio
import hashlib
import random
from dataclasses import dataclass
from typing import Protocol

from commerce_common.events import OrderLine
from fulfillment_svc.settings import FulfillmentSettings


@dataclass(frozen=True, slots=True)
class Shipment:
    carrier: str
    tracking_number: str


class RetryableFulfillmentError(Exception):
    """Provider timeout / 5xx: try again (same idempotency key)."""


class TerminalFulfillmentError(Exception):
    """Provider refused the order (e.g. item unavailable at the warehouse): compensate, don't retry."""


class FulfillmentProvider(Protocol):
    async def submit(self, *, idempotency_key: str, order_id: str, lines: list[OrderLine]) -> Shipment: ...


class MockProvider:
    """Accepts orders after a short delay. Failure injection is opt-in (settings) to demo compensation."""

    def __init__(self, settings: FulfillmentSettings) -> None:
        self._latency_s = settings.fulfillment_mock_latency_ms / 1000
        self._failure_rate = settings.fulfillment_mock_failure_rate
        self._fail_skus = set(settings.fulfillment_mock_fail_skus)

    async def submit(self, *, idempotency_key: str, order_id: str, lines: list[OrderLine]) -> Shipment:
        await asyncio.sleep(self._latency_s)
        failing = [line.sku_id for line in lines if line.sku_id in self._fail_skus]
        if failing:
            raise TerminalFulfillmentError(f"warehouse cannot fulfil: {', '.join(failing)}")
        if self._failure_rate and random.random() < self._failure_rate:  # noqa: S311 (chaos switch, not crypto)
            raise TerminalFulfillmentError("warehouse rejected the order (injected failure)")
        # Deterministic per idempotency key, like a real provider returning the same shipment on retry.
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:12].upper()
        return Shipment(carrier="MockShip", tracking_number=f"MS{digest}")


def build_provider(settings: FulfillmentSettings) -> FulfillmentProvider:
    return MockProvider(settings)
