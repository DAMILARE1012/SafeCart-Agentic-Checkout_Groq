"""Client for agent-svc: opens a turn stream (SSE) or reads conversation history."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from httpx_sse import EventSource

from commerce_common.errors import DomainError
from commerce_common.http import service_client


class UpstreamError(DomainError):
    """agent-svc rejected the request; carries its status and error body through."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("code", "upstream_error")), str(body.get("message", "Upstream error")))
        self.http_status = status if status < 500 else 502
        self.retryable = bool(body.get("retryable", status >= 500))


@dataclass
class TurnResult:
    """A fully collected turn (used by channels that can't stream, e.g. Telegram)."""

    message_id: str = ""
    text: str = ""
    blocks: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None


class AgentClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    @classmethod
    def create(cls, base_url: str, api_key: str, *, timeout_s: float) -> AgentClient:
        # Long read timeout: agent-svc sends keep-alives every 10 s while the model thinks.
        timeout = httpx.Timeout(timeout_s, read=60.0)
        return cls(service_client(base_url, api_key, timeout))

    async def aclose(self) -> None:
        await self._http.aclose()

    @asynccontextmanager
    async def open_turn(
        self, conversation_id: str, payload: dict[str, Any], *, request_id: str | None = None
    ) -> AsyncIterator[AsyncIterator[tuple[str, Any]]]:
        """Opens the stream and checks the status BEFORE yielding, so errors map to real HTTP statuses."""
        headers = {"Accept": "text/event-stream"}
        if request_id:
            headers["X-Request-Id"] = request_id
        request = self._http.build_request(
            "POST", f"/v1/conversations/{conversation_id}/turns", json=payload, headers=headers
        )
        try:
            response = await self._http.send(request, stream=True)
        except httpx.TransportError as exc:
            raise UpstreamError(
                503, {"code": "assistant_unavailable", "message": "Assistant unavailable"}
            ) from exc
        try:
            if response.status_code != 200:
                await response.aread()
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                raise UpstreamError(response.status_code, body)

            async def events() -> AsyncIterator[tuple[str, Any]]:
                async for sse in EventSource(response).aiter_sse():
                    yield sse.event, json.loads(sse.data) if sse.data else None

            yield events()
        finally:
            await response.aclose()

    async def collect_turn(self, conversation_id: str, payload: dict[str, Any]) -> TurnResult:
        result = TurnResult()
        async with self.open_turn(conversation_id, payload) as events:
            async for name, data in events:
                if name == "turn.started":
                    result.message_id = data["message_id"]
                elif name == "text.delta":
                    result.text += data["delta"]
                elif name == "block":
                    result.blocks.append(data["block"])
                elif name == "suggestions":
                    result.suggestions = data["suggestions"]
                elif name == "turn.error":
                    result.error = data
        return result

    async def history(self, conversation_id: str) -> dict[str, Any]:
        try:
            response = await self._http.get(f"/v1/conversations/{conversation_id}/messages")
        except httpx.TransportError as exc:
            raise UpstreamError(
                503, {"code": "assistant_unavailable", "message": "Assistant unavailable"}
            ) from exc
        if response.status_code != 200:
            raise UpstreamError(response.status_code, response.json() if response.content else {})
        body: dict[str, Any] = response.json()
        return body
