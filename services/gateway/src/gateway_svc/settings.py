from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator

from commerce_common.money import validate_currency
from commerce_common.settings import CsvList, ServiceSettings, split_csv


class GatewaySettings(ServiceSettings):
    otel_service_name: str = "gateway-svc"
    public_base_url: str = "http://localhost"
    redis_url: str | None = None  # rate limits + Telegram update de-duplication; in-process when unset

    # Upstream
    agent_service_url: str
    checkout_service_url: str
    service_api_key: SecretStr  # gateway's own key, presented to agent-svc and checkout-svc
    internal_http_timeout_seconds: float = 5.0

    # Widget sessions
    jwt_secret: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_session_ttl_minutes: int = 60
    widget_publishable_key: str
    widget_allowed_origins: CsvList = Field(default_factory=list)
    cors_allowed_origins: CsvList = Field(default_factory=list)

    # Merchant presentation (single-merchant MVP)
    merchant_name: str = "Northwind Outfitters"
    merchant_logo_url: str | None = None
    default_currency: str = "USD"
    supported_currencies: CsvList = Field(default_factory=lambda: ["USD"])

    # Abuse limits
    rate_limit_messages_per_minute: int = 20
    rate_limit_ip_requests_per_minute: int = 120
    rate_limit_checkouts_per_hour: int = 5

    # Telegram
    telegram_enabled: bool = False
    telegram_bot_token: SecretStr | None = None
    telegram_api_base_url: str = "https://api.telegram.org"
    telegram_update_mode: Literal["webhook", "polling"] = "polling"
    telegram_webhook_url: str | None = None
    telegram_webhook_secret: SecretStr | None = None
    telegram_allowed_updates: CsvList = Field(default_factory=lambda: ["message", "callback_query"])

    @field_validator(
        "widget_allowed_origins",
        "cors_allowed_origins",
        "supported_currencies",
        "telegram_allowed_updates",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: object) -> object:
        return split_csv(value)

    @field_validator("supported_currencies")
    @classmethod
    def _currencies(cls, values: list[str]) -> list[str]:
        return [validate_currency(v.upper()) for v in values]

    @field_validator(
        "telegram_bot_token",
        "telegram_webhook_secret",
        "merchant_logo_url",
        "telegram_webhook_url",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @model_validator(mode="after")
    def _consistency(self) -> GatewaySettings:
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        if self.is_production and secret.startswith("change-me"):
            raise ValueError("JWT_SECRET still has its placeholder value")
        if self.default_currency not in self.supported_currencies:
            raise ValueError("DEFAULT_CURRENCY must be listed in SUPPORTED_CURRENCIES")
        if self.telegram_enabled:
            if self.telegram_bot_token is None:
                raise ValueError("TELEGRAM_BOT_TOKEN is required when TELEGRAM_ENABLED=true")
            if self.telegram_update_mode == "webhook" and not (
                self.telegram_webhook_url and self.telegram_webhook_secret
            ):
                raise ValueError("webhook mode needs TELEGRAM_WEBHOOK_URL and TELEGRAM_WEBHOOK_SECRET")
        return self
