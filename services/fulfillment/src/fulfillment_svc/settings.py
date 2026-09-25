from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from commerce_common.settings import CsvList, ServiceSettings, split_csv


class FulfillmentSettings(ServiceSettings):
    otel_service_name: str = "fulfillment-svc"
    database_url: str

    # Messaging
    rabbitmq_url: str
    events_exchange: str = "commerce.events"
    events_dlx: str = "commerce.events.dlx"
    event_max_delivery_attempts: int = 8

    # Provider
    fulfillment_provider: Literal["mock"] = "mock"  # "http" (real 3PL) plugs in behind the same interface
    fulfillment_timeout_seconds: float = 15.0
    fulfillment_max_retries: int = 5
    fulfillment_mock_latency_ms: int = 800
    # Demo/chaos switches (default OFF): exercise the failure → refund path on purpose.
    fulfillment_mock_failure_rate: float = 0.0
    fulfillment_mock_fail_skus: CsvList = Field(default_factory=list)

    worker_health_port: int = 8001

    @field_validator("fulfillment_mock_fail_skus", mode="before")
    @classmethod
    def _csv(cls, value: object) -> object:
        return split_csv(value)

    @field_validator("fulfillment_mock_failure_rate")
    @classmethod
    def _rate(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("FULFILLMENT_MOCK_FAILURE_RATE must be between 0 and 1")
        return value
