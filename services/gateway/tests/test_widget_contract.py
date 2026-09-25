"""Consumer-driven contract: what the gateway itself produces must match the widget's published schema
(contracts/widget/gateway-api.schema.json, generated from widget/src/shared/api/contracts.ts)."""

from __future__ import annotations

from typing import Any

import httpx
from contract_kit import assert_matches_widget
from gateway_testkit import QUOTE, FakeAgent, new_session, turn_stream
from test_gateway import auth, parse


async def test_session_response_matches_the_widget(gateway: Any) -> None:
    async with gateway() as gw:
        session = await new_session(gw)
    assert_matches_widget("SessionResponse", session)


async def test_turn_stream_events_match_the_widget(gateway: Any, fake_agent: FakeAgent) -> None:
    fake_agent.reply = lambda _: httpx.Response(
        200,
        content=turn_stream(
            "Here is your summary", [{"type": "quote", "quote": QUOTE, "confirmation": None}], ["Checkout"]
        ),
        headers={"content-type": "text/event-stream"},
    )
    async with gateway() as gw:
        session = await new_session(gw)
        response = await gw.post(
            f"/v1/conversations/{session['conversation_id']}/turns",
            json={"client_message_id": "m_12345678", "text": "checkout"},
            headers=auth(session),
        )
    events = parse(response.text)
    assert {name for name, _ in events} >= {"turn.started", "text.delta", "block", "turn.completed"}
    for name, data in events:
        assert_matches_widget("TurnStreamEvent", {"event": name, "data": data})


async def test_confirm_and_errors_match_the_widget(gateway: Any) -> None:
    async with gateway() as gw:
        session = await new_session(gw)
        ok = await gw.post(
            "/v1/checkout/confirm",
            json={"confirmation_token": "g" * 30},
            headers={**auth(session), "Idempotency-Key": "k-1"},
        )
        missing_key = await gw.post(
            "/v1/checkout/confirm", json={"confirmation_token": "g" * 30}, headers=auth(session)
        )
        unauthenticated = await gw.get("/v1/orders/ord_1")
    assert_matches_widget("ConfirmCheckoutResponse", ok.json())
    for response in (missing_key, unauthenticated):
        assert response.status_code >= 400
        assert_matches_widget("ApiErrorBody", response.json())
