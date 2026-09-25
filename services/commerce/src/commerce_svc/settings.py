from __future__ import annotations

from typing import Literal

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator

from commerce_common.money import validate_currency
from commerce_common.settings import CsvList, ServiceSettings, require_sha256_hex, split_csv


class CommerceSettings(ServiceSettings):
    otel_service_name: str = "commerce-svc"
    database_url: str

    # Money
    default_currency: str = "USD"
    supported_currencies: CsvList = Field(default_factory=lambda: ["USD"])
    pricing_fx_fallback_enabled: bool = True
    fx_provider: Literal["static"] = "static"  # rates are seeded/managed in the fx_rates table
    fx_max_rate_age_minutes: int = 240

    # Tax & quotes
    tax_provider: Literal["static", "stripe_tax"] = "static"
    tax_default_behavior: Literal["exclusive", "inclusive"] = "exclusive"
    quote_ttl_minutes: int = 15
    inventory_reservation_ttl_minutes: int = 35  # > Stripe Checkout session lifetime (30 min)
    max_item_quantity: int = 20

    # Security
    field_encryption_key: SecretStr
    agent_svc_api_key_hash: str
    checkout_svc_api_key_hash: str

    # Housekeeping
    idempotency_ttl_hours: int = 24
    worker_health_port: int = 8001

    @field_validator("supported_currencies", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return split_csv(value)

    @field_validator("default_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        return validate_currency(value.upper())

    @field_validator("supported_currencies")
    @classmethod
    def _currencies(cls, values: list[str]) -> list[str]:
        return [validate_currency(v.upper()) for v in values]

    @field_validator("agent_svc_api_key_hash", "checkout_svc_api_key_hash")
    @classmethod
    def _hashes(cls, value: str, info: object) -> str:
        return require_sha256_hex(value, getattr(info, "field_name", "api key hash"))

    @field_validator("field_encryption_key")
    @classmethod
    def _fernet(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value())
        except Exception as exc:
            raise ValueError("FIELD_ENCRYPTION_KEY must be a urlsafe base64 Fernet key") from exc
        return value

    @model_validator(mode="after")
    def _consistency(self) -> CommerceSettings:
        if self.default_currency not in self.supported_currencies:
            raise ValueError("DEFAULT_CURRENCY must be listed in SUPPORTED_CURRENCIES")
        if self.tax_provider == "stripe_tax":
            raise ValueError("TAX_PROVIDER=stripe_tax arrives with checkout (M3); use 'static' for now")
        if not 1 <= self.max_item_quantity <= 999:
            raise ValueError("MAX_ITEM_QUANTITY must be between 1 and 999")
        return self
