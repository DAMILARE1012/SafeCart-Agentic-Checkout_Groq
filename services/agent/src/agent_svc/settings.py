from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, field_validator, model_validator

from commerce_common.settings import ServiceSettings, require_sha256_hex


class AgentSettings(ServiceSettings):
    otel_service_name: str = "agent-svc"

    # Conversation memory (LangGraph checkpointer). psycopg DSN: postgresql://user:pw@host/db
    database_url: str = ""
    langgraph_checkpointer: Literal["postgres", "memory"] = "postgres"
    redis_url: str | None = None  # per-conversation turn lock; in-process lock when unset (dev/tests)

    # Upstream services
    commerce_service_url: str
    checkout_service_url: str | None = None  # order status (read-only)
    service_api_key: SecretStr  # this service's own key, presented to commerce-svc
    gateway_svc_api_key_hash: str  # the only caller allowed to run turns
    internal_http_timeout_seconds: float = 5.0
    internal_http_max_retries: int = 2

    # LLM (Groq)
    groq_api_key: SecretStr
    groq_base_url: str = "https://api.groq.com"
    groq_model_primary: str = "openai/gpt-oss-120b"
    groq_model_fallback: str = "qwen/qwen3.8-27b"  # different family + separate rate-limit bucket
    groq_model_guard: str = "meta-llama/llama-prompt-guard-2-86m"  # purpose-built injection classifier
    groq_temperature: float = 0.1
    groq_max_tokens: int = 512
    groq_timeout_seconds: float = 20.0
    # Retries apply to the FALLBACK only: the primary fails over immediately (e.g. on 429) instead of
    # sleeping on Retry-After, because each model has its own quota.
    groq_max_retries: int = 1

    # Agent behaviour & guardrails
    agent_recursion_limit: int = 25
    agent_turn_timeout_seconds: float = 45.0
    agent_history_max_messages: int = 16
    agent_prompt_version: str = "v1"
    guardrail_input_classifier_enabled: bool = True
    guardrail_output_amount_validation_enabled: bool = True
    guardrail_max_regenerations: int = 1
    guardrail_injection_threshold: float = 0.5  # prompt-guard score above which input is refused

    @field_validator("gateway_svc_api_key_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        return require_sha256_hex(value, "GATEWAY_SVC_API_KEY_HASH")

    @field_validator("groq_api_key", "service_api_key")
    @classmethod
    def _non_empty(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> AgentSettings:
        if self.langgraph_checkpointer == "postgres" and not self.database_url:
            raise ValueError("DATABASE_URL is required when LANGGRAPH_CHECKPOINTER=postgres")
        if self.langgraph_checkpointer == "memory" and self.app_env in {"staging", "production"}:
            raise ValueError("LANGGRAPH_CHECKPOINTER=memory is for development/tests only")
        if self.agent_recursion_limit < 6:
            raise ValueError("AGENT_RECURSION_LIMIT must be ≥ 6 (guard → sync → agent → tools → …)")
        return self
