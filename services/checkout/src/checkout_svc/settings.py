from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, field_validator, model_validator

from commerce_common.settings import ServiceSettings, require_sha256_hex


class CheckoutSettings(ServiceSettings):
    otel_service_name: str = "checkout-svc"
    database_url: str
    public_base_url: str = "http://localhost"
    merchant_name: str = "Northwind Outfitters"

    # Upstream + callers
    commerce_service_url: str
    service_api_key: SecretStr  # checkout-svc's own key, presented to commerce-svc
    gateway_svc_api_key_hash: str
    agent_svc_api_key_hash: str
    internal_http_timeout_seconds: float = 5.0
    internal_http_max_retries: int = 2

    # Stripe: this is the ONLY service that ever receives these values.
    stripe_secret_key: SecretStr
    stripe_webhook_secret: SecretStr
    stripe_api_version: str = "2026-08-26.dahlia"
    stripe_webhook_tolerance_seconds: int = 300
    stripe_max_network_retries: int = 2
    checkout_mode: Literal["checkout_session"] = "checkout_session"
    checkout_success_path: str = "/checkout/success"
    checkout_cancel_path: str = "/checkout/cancel"
    checkout_session_expiry_minutes: int = 30

    # Business safety limits
    confirmation_token_ttl_minutes: int = 10
    order_created_stale_seconds: int = 120
    max_order_total_minor: int = 500_000

    worker_health_port: int = 8001

    # Messaging (fulfilment request/results). Only the worker uses RabbitMQ; it won't start without it.
    rabbitmq_url: str | None = None
    events_exchange: str = "commerce.events"
    events_dlx: str = "commerce.events.dlx"
    event_max_delivery_attempts: int = 8

    # Compensation, reconciliation and alerting (M5)
    compensation_auto_refund_enabled: bool = True  # False: failed orders go to MANUAL_REVIEW instead
    reconciliation_enabled: bool = True
    reconciliation_interval_seconds: int = 900
    reconciliation_lookback_hours: int = 48
    reconciliation_grace_minutes: int = 5  # younger orders are left to the webhooks
    fulfillment_stuck_after_minutes: int = 15
    alert_webhook_url: SecretStr | None = None  # Slack-compatible incoming webhook; empty = log only

    @field_validator("gateway_svc_api_key_hash", "agent_svc_api_key_hash")
    @classmethod
    def _hashes(cls, value: str, info: object) -> str:
        return require_sha256_hex(value, getattr(info, "field_name", "api key hash"))

    @field_validator("stripe_webhook_secret")
    @classmethod
    def _webhook_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret.startswith("whsec_") or len(secret) < 20:
            raise ValueError(
                "STRIPE_WEBHOOK_SECRET must be a real whsec_… secret (e.g. from `stripe listen`)"
            )
        return value

    @model_validator(mode="after")
    def _key_matches_environment(self) -> CheckoutSettings:
        key = self.stripe_secret_key.get_secret_value()
        if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")) or len(key) < 20:
            raise ValueError("STRIPE_SECRET_KEY is not a Stripe secret key")
        live = "_live_" in key
        # Fail fast on the most expensive misconfiguration there is: live keys outside production
        # (or test keys in production).
        if live and not self.is_production:
            raise ValueError("Live Stripe keys are only allowed when APP_ENV=production")
        if self.is_production and not live:
            raise ValueError("APP_ENV=production requires a live Stripe key")
        if not 30 <= self.checkout_session_expiry_minutes <= 1440:
            raise ValueError("CHECKOUT_SESSION_EXPIRY_MINUTES must be between 30 and 1440 (Stripe limits)")
        return self
