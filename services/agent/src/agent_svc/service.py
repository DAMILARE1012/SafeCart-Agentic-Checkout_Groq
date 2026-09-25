"""Runs one conversation turn through the graph and turns the result into SSE events.

Why the text isn't streamed token-by-token from the LLM: the output guard must validate
every amount BEFORE the customer sees it. Groq generates the full reply in well under a
second, and the reply is then streamed out in small chunks, so it still feels live.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import structlog
from langchain_core.messages import RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field

from agent_svc.checkout_reader import CheckoutReader
from agent_svc.commerce_client import CommerceClient
from agent_svc.graph import UNAVAILABLE, turn_input
from agent_svc.settings import AgentSettings
from agent_svc.state import AgentContext, AgentState
from commerce_common.sse import sse_comment, sse_event

log = structlog.get_logger("agent.turns")

KEEPALIVE_S = 10.0
_CHUNK_RE = re.compile(r"\S+\s*")


class TurnContext(BaseModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    locale: str = Field(default="en-US", max_length=35)
    channel: Literal["web", "telegram"] = "web"
    merchant_name: str = Field(default="our store", max_length=120)


class TurnRequest(BaseModel):
    client_message_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_\-:.]+$")
    text: str = Field(max_length=1000)
    action: dict[str, Any] | None = None
    context: TurnContext


def text_chunks(text: str, words_per_chunk: int = 3) -> list[str]:
    words = _CHUNK_RE.findall(text)
    return ["".join(words[i : i + words_per_chunk]) for i in range(0, len(words), words_per_chunk)]


def reply_events(
    message_id: str, text: str, blocks: list[dict[str, Any]], suggestions: list[str]
) -> list[bytes]:
    events = [sse_event("text.delta", {"message_id": message_id, "delta": c}) for c in text_chunks(text)]
    events += [sse_event("block", {"message_id": message_id, "block": b}) for b in blocks]
    if suggestions:
        events.append(sse_event("suggestions", {"message_id": message_id, "suggestions": suggestions}))
    events.append(sse_event("turn.completed", {"message_id": message_id}))
    return events


class TurnService:
    def __init__(
        self,
        graph: CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState],
        settings: AgentSettings,
        commerce: CommerceClient,
        checkout: CheckoutReader | None = None,
    ) -> None:
        self._graph = graph
        self._settings = settings
        self._commerce = commerce
        self._checkout = checkout

    def _config(self, conversation_id: str) -> RunnableConfig:
        return {
            "configurable": {"thread_id": conversation_id},
            "recursion_limit": self._settings.agent_recursion_limit,
        }

    async def history(self, conversation_id: str) -> list[dict[str, Any]]:
        snapshot = await self._graph.aget_state(self._config(conversation_id))
        transcript: list[dict[str, Any]] = snapshot.values.get("transcript", []) if snapshot.values else []
        return [{k: v for k, v in entry.items() if k != "reply_to"} for entry in transcript]

    async def _completed_reply(self, conversation_id: str, client_message_id: str) -> dict[str, Any] | None:
        snapshot = await self._graph.aget_state(self._config(conversation_id))
        for entry in (snapshot.values or {}).get("transcript", []):
            if entry.get("reply_to") == client_message_id:
                return dict(entry)
        return None

    async def stream(self, conversation_id: str, request: TurnRequest) -> AsyncIterator[bytes]:
        """SSE bytes for one turn. The caller holds the conversation lock for the duration."""
        # Idempotent replay: same client_message_id → same reply, without re-running the model or tools.
        if (done := await self._completed_reply(conversation_id, request.client_message_id)) is not None:
            log.info("turn_replayed", conversation_id=conversation_id)
            yield sse_event("turn.started", {"turn_id": "replay", "message_id": done["id"]})
            for event in reply_events(done["id"], done["text"], done["blocks"], done.get("suggestions", [])):
                yield event
            return

        turn_id = uuid.uuid4().hex
        message_id = f"msg_{uuid.uuid4().hex}"
        structlog.contextvars.bind_contextvars(conversation_id=conversation_id, turn_id=turn_id)
        yield sse_event("turn.started", {"turn_id": turn_id, "message_id": message_id})

        ctx = AgentContext(
            conversation_id=conversation_id,
            client_message_id=request.client_message_id,
            currency=request.context.currency,
            locale=request.context.locale,
            channel=request.context.channel,
            merchant_name=request.context.merchant_name,
            commerce=self._commerce,
            checkout=self._checkout,
        )
        graph_input = turn_input(
            user_text=request.text,
            action=request.action,
            client_message_id=request.client_message_id,
            turn_id=turn_id,
            assistant_message_id=message_id,
        )
        run = asyncio.create_task(self._run(conversation_id, graph_input, ctx))
        try:
            # Keep proxies/load balancers from closing the stream while the model thinks.
            while not run.done():
                done_set, _ = await asyncio.wait({run}, timeout=KEEPALIVE_S)
                if not done_set:
                    yield sse_comment()
            output = run.result()
        finally:
            if not run.done():
                run.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run

        if "error" in output:
            yield sse_event("turn.error", {**output["error"], "message_id": message_id})
            return
        for event in reply_events(
            output["message_id"], output["text"], output["blocks"], output["suggestions"]
        ):
            yield event

    async def _run(
        self, conversation_id: str, graph_input: dict[str, Any], ctx: AgentContext
    ) -> dict[str, Any]:
        config = self._config(conversation_id)
        try:
            async with asyncio.timeout(self._settings.agent_turn_timeout_seconds):
                result = await self._graph.ainvoke(cast(AgentState, graph_input), config, context=ctx)
            output: dict[str, Any] | None = result.get("turn_output")
            if not output:
                raise RuntimeError("graph finished without output")
            if "error" not in output:
                log.info("turn_completed", blocks=[b["type"] for b in output["blocks"]])
            return output
        except Exception as exc:
            log.error("turn_failed", error=type(exc).__name__, detail=str(exc)[:300])
            await self._discard_turn(conversation_id, ctx.client_message_id)
            return {"error": UNAVAILABLE}

    async def _discard_turn(self, conversation_id: str, client_message_id: str) -> None:
        """After a crash/timeout, remove this turn's partial messages so a retry starts clean."""
        config = self._config(conversation_id)
        try:
            snapshot = await self._graph.aget_state(config)
            messages = (snapshot.values or {}).get("messages", [])
            start = next((i for i, m in enumerate(messages) if m.id == client_message_id), None)
            if start is not None:
                await self._graph.aupdate_state(
                    config,
                    {
                        "messages": [RemoveMessage(id=str(m.id)) for m in messages[start:]],
                        "turn_output": None,
                    },
                    as_node="render",
                )
        except Exception as exc:
            log.warning("discard_turn_failed", error=type(exc).__name__)
