"""LLM access behind a small protocol, so the graph can run against Groq or a scripted test model."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agent_svc.settings import AgentSettings

GUARD_MAX_CHARS = 1800  # prompt-guard context is 512 tokens


class GuardVerdict(BaseModel):
    """Input screening result."""

    allowed: bool
    category: Literal["ok", "prompt_injection", "abuse"] = "ok"
    score: float | None = None


class ModelProvider(Protocol):
    def chat(self, tools: Sequence[BaseTool]) -> Runnable[LanguageModelInput, AIMessage]: ...

    async def screen(self, text: str) -> GuardVerdict: ...


def model_options(model: str) -> dict[str, Any]:
    """Per-family knobs: keep reasoning short (it costs tokens and latency) and out of the reply text."""
    if model.startswith("openai/gpt-oss"):
        return {"reasoning_effort": "low"}
    if model.startswith("qwen/qwen3"):
        return {"reasoning_effort": "none"}
    return {}


def is_rate_limited(exc: BaseException) -> bool:
    """True if the (final) model failure was a rate limit, so we can tell the user to retry shortly."""
    seen: BaseException | None = exc
    while seen is not None:
        if type(seen).__name__ == "RateLimitError" or getattr(seen, "status_code", None) == 429:
            return True
        seen = seen.__cause__ or seen.__context__
    return False


class GroqModels:
    """Primary model for tool use with an immediate cross-model fallback; prompt-guard for input screening."""

    def __init__(self, settings: AgentSettings) -> None:
        from langchain_groq import ChatGroq  # imported lazily: tests never need the Groq SDK

        common: dict[str, Any] = {
            "api_key": settings.groq_api_key,
            "base_url": settings.groq_base_url,
            "temperature": settings.groq_temperature,
            "max_tokens": settings.groq_max_tokens,
            "timeout": settings.groq_timeout_seconds,
        }
        # Primary: no SDK retries, so a 429/5xx fails over to the fallback at once (separate quota).
        self._primary = ChatGroq(
            model=settings.groq_model_primary,
            max_retries=0,
            **common,
            **model_options(settings.groq_model_primary),
        )
        self._fallback = ChatGroq(
            model=settings.groq_model_fallback,
            max_retries=settings.groq_max_retries,
            **common,
            **model_options(settings.groq_model_fallback),
        )
        self._guard = ChatGroq(
            model_name=settings.groq_model_guard,
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
            timeout=5.0,
            max_retries=0,
        )
        self._threshold = settings.guardrail_injection_threshold
        self._cache: dict[frozenset[str], Runnable[LanguageModelInput, AIMessage]] = {}

    def chat(self, tools: Sequence[BaseTool]) -> Runnable[LanguageModelInput, AIMessage]:
        key = frozenset(t.name for t in tools)
        if key not in self._cache:
            if tools:
                primary: Runnable[LanguageModelInput, AIMessage] = self._primary.bind_tools(tools)
                fallback: Runnable[LanguageModelInput, AIMessage] = self._fallback.bind_tools(tools)
            else:
                primary, fallback = self._primary, self._fallback
            self._cache[key] = primary.with_fallbacks([fallback])
        return self._cache[key]

    async def screen(self, text: str) -> GuardVerdict:
        """Llama Prompt Guard 2 returns the probability that the text is a prompt attack."""
        reply = await self._guard.ainvoke([HumanMessage(text[:GUARD_MAX_CHARS])])
        score = float(str(reply.content).strip())
        blocked = score >= self._threshold
        return GuardVerdict(
            allowed=not blocked, category="prompt_injection" if blocked else "ok", score=score
        )
