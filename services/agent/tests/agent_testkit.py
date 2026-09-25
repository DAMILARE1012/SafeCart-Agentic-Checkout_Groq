"""Shared test kit for agent-svc (helpers + fixtures).

Agent tests run the REAL graph and the REAL commerce-svc (in-process, on a real Postgres).

Only the LLM is scripted: each test states exactly what the model "decides", so we can
assert everything the deterministic code around it does.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableLambda
from testcontainers.community.postgres import PostgresContainer

from agent_svc.commerce_client import CommerceClient
from agent_svc.main import create_app
from agent_svc.models import GuardVerdict
from agent_svc.settings import AgentSettings
from commerce_common.auth import hash_api_key
from commerce_common.db import create_engine, create_session_factory
from commerce_svc.main import create_app as create_commerce_app
from commerce_svc.migrate import upgrade
from commerce_svc.seed import seed
from commerce_svc.settings import CommerceSettings

GATEWAY_KEY = "gateway-test-key"
AGENT_KEY = "agent-test-key"

Reply = AIMessage | Exception | Callable[[list[BaseMessage], list[str]], AIMessage]


class ScriptedModels:
    """Stands in for Groq. Records which tools were offered and what the model was sent."""

    def __init__(self, replies: list[Reply], verdict: GuardVerdict | None = None) -> None:
        self.replies = list(replies)
        self.verdict = verdict or GuardVerdict(allowed=True)
        self.calls: list[dict[str, Any]] = []
        self.screened: list[str] = []

    def chat(self, tools: Any) -> RunnableLambda[Any, AIMessage]:
        names = [t.name for t in tools]

        async def run(messages: list[BaseMessage]) -> AIMessage:
            self.calls.append({"tools": names, "messages": messages})
            if not self.replies:
                raise AssertionError("scripted model ran out of replies")
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            if callable(reply):
                return reply(messages, names)
            return reply

        return RunnableLambda(run)

    async def screen(self, text: str) -> GuardVerdict:
        self.screened.append(text)
        return self.verdict


def call(name: str, args: dict[str, Any] | None = None, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args or {}, "id": call_id, "type": "tool_call"}]
    )


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


# ---------------------------------------------------------------------------
# commerce-svc on a real Postgres (session-scoped)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def commerce_db_url() -> Iterator[str]:
    with PostgresContainer("pgvector/pgvector:0.8.6-pg18", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
async def commerce_app(commerce_db_url: str) -> Any:
    await asyncio.to_thread(upgrade, commerce_db_url)
    engine = create_engine(commerce_db_url, pool_size=1, max_overflow=0)
    async with create_session_factory(engine)() as session, session.begin():
        await seed(session)
    await engine.dispose()
    return create_commerce_app(
        CommerceSettings(
            database_url=commerce_db_url,
            app_env="test",
            log_level="WARNING",
            log_format="console",
            supported_currencies=["USD", "EUR", "GBP", "NGN", "JPY"],
            field_encryption_key=Fernet.generate_key().decode(),
            agent_svc_api_key_hash=hash_api_key(AGENT_KEY),
            checkout_svc_api_key_hash=hash_api_key("unused"),
        )
    )


@pytest.fixture
def agent_settings() -> AgentSettings:
    return AgentSettings(
        app_env="test",
        log_level="WARNING",
        log_format="console",
        langgraph_checkpointer="memory",
        commerce_service_url="http://commerce",
        service_api_key=AGENT_KEY,
        gateway_svc_api_key_hash=hash_api_key(GATEWAY_KEY),
        groq_api_key="unused-in-tests",
    )


@pytest.fixture
def make_agent(commerce_app: Any, agent_settings: AgentSettings) -> Callable[..., Any]:
    """Builds an agent-svc app (scripted LLM + in-process commerce) and an HTTP client for it."""

    @asynccontextmanager
    async def factory(models: ScriptedModels, **overrides: Any) -> AsyncIterator[httpx.AsyncClient]:
        settings = agent_settings.model_copy(update=overrides)
        commerce = CommerceClient(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=commerce_app),
                base_url="http://commerce",
                headers={"Authorization": f"Bearer {AGENT_KEY}"},
            )
        )
        app = create_app(settings, models=models, commerce=commerce)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://agent",
                headers={"Authorization": f"Bearer {GATEWAY_KEY}"},
                timeout=30,
            ) as client,
        ):
            client.app = app  # type: ignore[attr-defined]
            yield client

    return factory


def parse_sse(body: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for frame in body.split("\n\n"):
        name, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


def turn_body(
    text: str, message_id: str, action: dict[str, Any] | None = None, currency: str = "USD"
) -> dict[str, Any]:
    return {
        "client_message_id": message_id,
        "text": text,
        "action": action,
        "context": {"currency": currency, "locale": "en-US", "channel": "web", "merchant_name": "Northwind"},
    }


async def run_turn(
    client: httpx.AsyncClient, conversation: str, body: dict[str, Any]
) -> list[tuple[str, Any]]:
    response = await client.post(f"/v1/conversations/{conversation}/turns", json=body)
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


def text_of(events: list[tuple[str, Any]]) -> str:
    return "".join(d["delta"] for e, d in events if e == "text.delta")


def blocks_of(events: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [d["block"] for e, d in events if e == "block"]
