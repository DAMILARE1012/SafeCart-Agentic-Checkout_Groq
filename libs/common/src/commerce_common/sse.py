"""Server-Sent Events framing, shared by agent-svc (producer) and gateway-svc (proxy)."""

from __future__ import annotations

import json
from typing import Any

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so events flush immediately
}


def sse_event(event: str, data: Any) -> bytes:
    payload = json.dumps(data, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {payload}\n\n".encode()


def sse_comment(text: str = "keep-alive") -> bytes:
    """Comment line: ignored by clients, keeps idle connections from being closed by proxies."""
    return f": {text}\n\n".encode()
