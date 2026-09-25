"""agent-svc HTTP API. Internal: only gateway-svc may call it (scope ``agent:turns``)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse

from agent_svc.service import TurnRequest, TurnService
from agent_svc.settings import AgentSettings
from agent_svc.turn_lock import TurnLock
from commerce_common.auth import require_scope
from commerce_common.errors import Conflict
from commerce_common.sse import SSE_HEADERS

CALLER_SCOPES = {"gateway-svc": frozenset({"agent:turns"})}

router = APIRouter(
    prefix="/v1/conversations", tags=["conversations"], dependencies=[Depends(require_scope("agent:turns"))]
)
ConversationId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_\-:]{1,64}$")]


def _service(request: Request) -> TurnService:
    service: TurnService = request.app.state.turn_service
    return service


@router.post("/{conversation_id}/turns")
async def run_turn(conversation_id: ConversationId, body: TurnRequest, request: Request) -> StreamingResponse:
    """Streams one turn as SSE.

    Events: turn.started → text.delta* → block* → suggestions? → turn.completed | turn.error.
    """
    settings: AgentSettings = request.app.state.settings
    lock: TurnLock = request.app.state.turn_lock
    ttl = int(settings.agent_turn_timeout_seconds) + 15
    token = await lock.acquire(conversation_id, ttl)
    if token is None:
        # Checked before streaming starts, so the caller gets a real 409 status.
        raise Conflict("turn_in_progress", "Still answering your previous message", details=None)

    service = _service(request)

    async def events() -> AsyncIterator[bytes]:
        try:
            async for chunk in service.stream(conversation_id, body):
                yield chunk
        finally:
            await lock.release(conversation_id, token)

    return StreamingResponse(events(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{conversation_id}/messages")
async def get_messages(conversation_id: ConversationId, request: Request) -> dict[str, Any]:
    return {"messages": await _service(request).history(conversation_id)}
