"""Base settings every service extends (design principle P8).

Values come only from environment variables (Compose passes each container
exactly the variables it needs from the single root ``.env``). Invalid
configuration raises at startup: the service refuses to boot.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")

# Comma-separated env values ("USD,EUR") → list[str]
CsvList = Annotated[list[str], NoDecode]


def split_csv(value: object) -> object:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def require_sha256_hex(value: str, field: str) -> str:
    if not _SHA256_HEX.fullmatch(value):
        raise ValueError(
            f"{field} must be a lowercase SHA-256 hex digest (64 chars). "
            "Generate a key+hash pair with the command in .env.example."
        )
    return value


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", frozen=True, case_sensitive=False)

    app_env: Literal["development", "test", "staging", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    default_merchant_id: str = "merchant_demo"

    otel_enabled: bool = False
    otel_service_name: str = "unknown-svc"
    otel_exporter_otlp_endpoint: str = "http://otel-collector:4317"
    metrics_enabled: bool = True

    database_pool_size: int = 10
    database_max_overflow: int = 5

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"
