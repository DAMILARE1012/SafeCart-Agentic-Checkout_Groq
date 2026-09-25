"""Service-to-service authentication with per-caller scopes (docs §3.4).

Each CALLER holds its own plaintext key. Each CALLEE holds only SHA-256 hashes
of the keys it accepts, mapped to scopes. Because no service ever holds another
service's plaintext key, a compromised service cannot impersonate a different
caller (e.g. agent-svc cannot present gateway-svc's confirm permission).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from fastapi import Request

from .errors import Forbidden, Unauthorized


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CallerPolicy:
    name: str
    key_hash: str
    scopes: frozenset[str]


class ServiceAuth:
    def __init__(self, callers: Iterable[CallerPolicy]) -> None:
        self._callers = tuple(callers)

    def authenticate(self, presented_key: str) -> CallerPolicy:
        digest = hash_api_key(presented_key)
        matched: CallerPolicy | None = None
        # Compare against every caller (no early exit) with constant-time comparison.
        for caller in self._callers:
            if hmac.compare_digest(digest, caller.key_hash):
                matched = caller
        if matched is None:
            raise Unauthorized("unauthorized", "Invalid service credentials")
        return matched


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("unauthorized", "Missing service credentials")
    return token.strip()


def require_scope(scope: str) -> Callable[[Request], Awaitable[str]]:
    """FastAPI dependency: authenticates the caller and enforces ``scope``. Returns the caller name."""

    async def dependency(request: Request) -> str:
        auth: ServiceAuth = request.app.state.service_auth
        caller = auth.authenticate(_bearer(request))
        if scope not in caller.scopes:
            raise Forbidden("forbidden", f"Caller '{caller.name}' lacks scope '{scope}'")
        request.state.caller = caller.name
        return caller.name

    return dependency
