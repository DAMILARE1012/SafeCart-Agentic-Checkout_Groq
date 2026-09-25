"""Widget sessions: publishable key + origin allowlist → short-lived JWT bound to one conversation."""

from __future__ import annotations

import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Request

from commerce_common.errors import Forbidden, Unauthorized
from gateway_svc.settings import GatewaySettings

AUDIENCE = "widget"
ISSUER = "gateway-svc"
WEB_CONVERSATION = re.compile(r"^web_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class WidgetSession:
    conversation_id: str
    currency: str
    locale: str


def new_conversation_id() -> str:
    return f"web_{uuid.uuid4().hex}"


def check_origin(request: Request, settings: GatewaySettings) -> None:
    """Browsers always send Origin on cross-origin POSTs; only allow-listed storefronts may start sessions."""
    origin = request.headers.get("origin")
    if origin is None and settings.app_env in {"development", "test"}:
        return  # curl / local tooling
    if origin not in settings.widget_allowed_origins:
        raise Forbidden("origin_not_allowed", "This site is not allowed to use the assistant")


def check_publishable_key(presented: str, settings: GatewaySettings) -> None:
    if not hmac.compare_digest(presented.encode(), settings.widget_publishable_key.encode()):
        raise Unauthorized("invalid_publishable_key", "Unknown publishable key")


def issue(session: WidgetSession, settings: GatewaySettings) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.jwt_session_ttl_minutes)
    claims = {
        "sub": session.conversation_id,
        "cur": session.currency,
        "loc": session.locale,
        "iat": now,
        "exp": expires,
        "aud": AUDIENCE,
        "iss": ISSUER,
    }
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)
    return token, expires


def verify(token: str, settings: GatewaySettings) -> WidgetSession:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # pinned: never trust the token's own "alg"
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("session_expired", "Session expired") from exc
    except jwt.InvalidTokenError as exc:
        raise Unauthorized("invalid_session", "Invalid session") from exc
    return WidgetSession(conversation_id=claims["sub"], currency=claims["cur"], locale=claims["loc"])


def session_from_request(request: Request, conversation_id: str | None = None) -> WidgetSession:
    settings: GatewaySettings = request.app.state.settings
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("invalid_session", "Missing session token")
    session = verify(token.strip(), settings)
    if conversation_id is not None and conversation_id != session.conversation_id:
        raise Forbidden("forbidden", "This session belongs to a different conversation")
    return session
