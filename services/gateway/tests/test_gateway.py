from __future__ import annotations

import json
from typing import Any

import httpx
import jwt
from gateway_testkit import (
    ORIGIN,
    PRODUCT,
    PUBLISHABLE_KEY,
    QUOTE,
    FakeAgent,
    FakeBot,
    FakeCheckout,
    new_session,
    turn_stream,
)
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

from commerce_common.sse import sse_event


def parse(body: str) -> list[tuple[str, Any]]:
    out = []
    for frame in body.split("\n\n"):
        lines = dict(
            line.split(": ", 1) for line in frame.splitlines() if not line.startswith(":") and ": " in line
        )
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


def auth(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['session_token']}"}


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
async def test_session_requires_allowed_origin_and_valid_key(gateway: Any) -> None:
    async with gateway() as gw:
        bad_origin = await gw.post(
            "/v1/sessions",
            json={"publishable_key": PUBLISHABLE_KEY},
            headers={"Origin": "https://evil.example"},
        )
        bad_key = await gw.post(
            "/v1/sessions", json={"publishable_key": "pk_wrong"}, headers={"Origin": ORIGIN}
        )
        session = await new_session(gw, currency="EUR")
    assert bad_origin.status_code == 403 and bad_origin.json()["code"] == "origin_not_allowed"
    assert bad_key.status_code == 401
    assert session["conversation_id"].startswith("web_")
    assert session["merchant"]["currency"] == "EUR"


async def test_unsupported_currency_falls_back_and_resume_keeps_conversation(gateway: Any) -> None:
    async with gateway() as gw:
        first = await new_session(gw, currency="JPY")
        resumed = await new_session(gw, resume_conversation_id=first["conversation_id"])
        forged = await new_session(gw, resume_conversation_id="tg_12345")  # not a web conversation id
    assert first["merchant"]["currency"] == "USD"
    assert resumed["conversation_id"] == first["conversation_id"]
    assert forged["conversation_id"] != "tg_12345"


async def test_token_is_bound_to_its_conversation_and_expires(gateway: Any) -> None:
    async with gateway() as gw:
        session = await new_session(gw)
        other = await gw.get("/v1/conversations/web_" + "0" * 32 + "/messages", headers=auth(session))
        expired_token = jwt.encode(
            {
                "sub": session["conversation_id"],
                "cur": "USD",
                "loc": "en",
                "exp": 1,
                "aud": "widget",
                "iss": "gateway-svc",
            },
            "x" * 40,
            algorithm="HS256",
        )
        expired = await gw.get(
            f"/v1/conversations/{session['conversation_id']}/messages",
            headers={"Authorization": f"Bearer {expired_token}"},
        )
        forged = jwt.encode({"sub": session["conversation_id"]}, "wrong-secret-" * 4, algorithm="HS256")
        tampered = await gw.get(
            f"/v1/conversations/{session['conversation_id']}/messages",
            headers={"Authorization": f"Bearer {forged}"},
        )
    assert other.status_code == 403
    assert expired.status_code == 401 and expired.json()["code"] == "session_expired"
    assert tampered.status_code == 401


async def test_cors_preflight_for_the_storefront(gateway: Any) -> None:
    async with gateway() as gw:
        response = await gw.options(
            "/v1/sessions",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization,idempotency-key",
            },
        )
    assert response.headers["access-control-allow-origin"] == ORIGIN


# ---------------------------------------------------------------------------
# Turns
# ---------------------------------------------------------------------------
async def test_turn_is_proxied_with_session_context(gateway: Any, fake_agent: FakeAgent) -> None:
    fake_agent.reply = lambda _: httpx.Response(
        200,
        content=turn_stream(
            "Here you go", [{"type": "quote", "quote": QUOTE, "confirmation": None}], ["Checkout"]
        ),
        headers={"content-type": "text/event-stream"},
    )
    async with gateway() as gw:
        session = await new_session(gw, currency="EUR")
        body = {
            "client_message_id": "m_12345678",
            "text": "checkout",
            "action": {"type": "start_checkout"},
        }
        response = await gw.post(
            f"/v1/conversations/{session['conversation_id']}/turns",
            json=body,
            headers={**auth(session), "Idempotency-Key": "m_12345678"},
        )
    events = parse(response.text)
    assert response.status_code == 200
    assert [e for e, _ in events][0] == "turn.started" and events[-1][0] == "turn.completed"
    assert any(e == "block" and d["block"]["type"] == "quote" for e, d in events)
    conversation, payload = fake_agent.turns[0]
    assert conversation == session["conversation_id"]
    assert payload["context"] == {
        "currency": "EUR",
        "locale": "en-US",
        "channel": "web",
        "merchant_name": "Northwind Outfitters",
    }
    assert payload["action"] == {"type": "start_checkout"}


async def test_unknown_actions_are_rejected_at_the_edge(gateway: Any) -> None:
    async with gateway() as gw:
        session = await new_session(gw)
        response = await gw.post(
            f"/v1/conversations/{session['conversation_id']}/turns",
            json={"client_message_id": "m_12345678", "text": "x", "action": {"type": "refund_order"}},
            headers=auth(session),
        )
    assert response.status_code == 422


async def test_agent_conflict_maps_to_409(gateway: Any, fake_agent: FakeAgent) -> None:
    fake_agent.reply = lambda _: httpx.Response(
        409, json={"code": "turn_in_progress", "message": "busy", "retryable": True}
    )
    async with gateway() as gw:
        session = await new_session(gw)
        response = await gw.post(
            f"/v1/conversations/{session['conversation_id']}/turns",
            json={"client_message_id": "m_12345678", "text": "hi"},
            headers=auth(session),
        )
    assert response.status_code == 409
    assert response.json()["code"] == "turn_in_progress"


async def test_per_conversation_rate_limit(gateway: Any) -> None:
    async with gateway() as gw:
        session = await new_session(gw)
        statuses = []
        for i in range(7):
            r = await gw.post(
                f"/v1/conversations/{session['conversation_id']}/turns",
                json={"client_message_id": f"m_0000000{i}", "text": "hi"},
                headers=auth(session),
            )
            statuses.append(r.status_code)
    assert statuses[:5] == [200] * 5
    assert statuses[5:] == [429, 429]


async def test_quote_blocks_get_a_grant_from_checkout_not_the_agent(
    gateway: Any, fake_agent: FakeAgent, fake_checkout: FakeCheckout
) -> None:
    fake_agent.reply = lambda _: httpx.Response(
        200,
        content=turn_stream("Summary", [{"type": "quote", "quote": QUOTE, "confirmation": None}]),
        headers={"content-type": "text/event-stream"},
    )
    async with gateway() as gw:
        session = await new_session(gw)
        response = await gw.post(
            f"/v1/conversations/{session['conversation_id']}/turns",
            json={"client_message_id": "m_12345678", "text": "checkout"},
            headers=auth(session),
        )
    block = next(d["block"] for e, d in parse(response.text) if e == "block")
    assert block["confirmation"] == {"token": "grant-" + "t" * 30, "expires_at": "2030-01-01T00:10:00Z"}
    assert fake_checkout.grants == [{"quote_id": "quote_1", "conversation_id": session["conversation_id"]}]


async def test_unpayable_quote_is_sent_without_a_grant(
    gateway: Any, fake_agent: FakeAgent, fake_checkout: FakeCheckout
) -> None:
    fake_checkout.quote_payable = False
    fake_agent.reply = lambda _: httpx.Response(
        200,
        content=turn_stream("Summary", [{"type": "quote", "quote": QUOTE, "confirmation": None}]),
        headers={"content-type": "text/event-stream"},
    )
    async with gateway() as gw:
        session = await new_session(gw)
        response = await gw.post(
            f"/v1/conversations/{session['conversation_id']}/turns",
            json={"client_message_id": "m_12345678", "text": "checkout"},
            headers=auth(session),
        )
    block = next(d["block"] for e, d in parse(response.text) if e == "block")
    assert block["confirmation"] is None
    assert parse(response.text)[-1][0] == "turn.completed"  # the turn itself still succeeds


async def test_confirm_goes_to_checkout_with_the_sessions_conversation(
    gateway: Any, fake_checkout: FakeCheckout
) -> None:
    async with gateway() as gw:
        session = await new_session(gw)
        missing_key = await gw.post(
            "/v1/checkout/confirm", json={"confirmation_token": "g" * 30}, headers=auth(session)
        )
        ok = await gw.post(
            "/v1/checkout/confirm",
            json={"confirmation_token": "g" * 30},
            headers={**auth(session), "Idempotency-Key": "stable-per-quote"},
        )
        order = await gw.get("/v1/orders/ord_1", headers=auth(session))
    assert missing_key.status_code == 422
    assert ok.json() == {"order_id": "ord_1", "checkout_url": "https://checkout.stripe.test/x"}
    assert fake_checkout.confirms == [
        ({"confirmation_token": "g" * 30, "conversation_id": session["conversation_id"]}, "stable-per-quote")
    ]
    assert order.json()["owner"] == session["conversation_id"]  # order reads are scoped to the session


async def test_checkout_confirm_is_rate_limited(gateway: Any) -> None:
    async with gateway(rate_limit_checkouts_per_hour=2) as gw:
        session = await new_session(gw)
        codes = []
        for i in range(3):
            r = await gw.post(
                "/v1/checkout/confirm",
                json={"confirmation_token": "g" * 30},
                headers={**auth(session), "Idempotency-Key": f"k{i}"},
            )
            codes.append(r.status_code)
    assert codes == [200, 200, 429]


async def test_stripe_return_pages(gateway: Any) -> None:
    async with gateway() as gw:
        success = await gw.get("/checkout/success?order_id=ord_1")
        cancel = await gw.get("/checkout/cancel")
    assert success.status_code == 200 and "confirming your payment" in success.text
    assert "Nothing was charged" in cancel.text


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg_message(update_id: int, text: str, chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "is_bot": False, "first_name": "Ada", "language_code": "en"},
            "text": text,
        },
    }


def tg_button(update_id: int, data: str, chat_id: int = 42) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "chat_instance": "ci",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Ada", "language_code": "en"},
            "data": data,
            "message": {
                "message_id": 1,
                "date": 1700000000,
                "chat": {"id": chat_id, "type": "private"},
                "text": "x",
            },
        },
    }


async def post_update(
    gw: httpx.AsyncClient, update: dict[str, Any], secret: str = "tg-secret"
) -> httpx.Response:
    response = await gw.post(
        "/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret}
    )
    await gw.app.state.telegram.drain()  # type: ignore[attr-defined]
    return response


async def test_telegram_webhook_requires_secret(gateway: Any, fake_agent: FakeAgent) -> None:
    async with gateway() as gw:
        response = await post_update(gw, tg_message(1, "hi"), secret="wrong")
    assert response.status_code == 403
    assert fake_agent.turns == []


async def test_telegram_products_render_as_photos_with_buttons(
    gateway: Any, fake_agent: FakeAgent, fake_bot: FakeBot
) -> None:
    fake_agent.reply = lambda _: httpx.Response(
        200,
        content=turn_stream(
            "Here are some shoes", [{"type": "product_list", "products": [PRODUCT]}], ["Checkout"]
        ),
        headers={"content-type": "text/event-stream"},
    )
    async with gateway() as gw:
        await post_update(gw, tg_message(100, "running shoes"))

    conversation, payload = fake_agent.turns[0]
    assert conversation == "tg_42"
    assert payload["client_message_id"] == "tg_100" and payload["context"]["channel"] == "telegram"
    kinds = [k for k, _ in fake_bot.sent]
    assert kinds == ["action", "message", "photo"]
    text_msg, photo = fake_bot.sent[1][1], fake_bot.sent[2][1]
    assert isinstance(text_msg["reply_markup"], ReplyKeyboardMarkup)  # suggestions as quick replies
    assert "$129.00  (was $149.00)" in photo["caption"]
    keyboard: InlineKeyboardMarkup = photo["reply_markup"]
    assert [b.callback_data for b in keyboard.inline_keyboard[0]] == [
        "add:sku_trail_gtx_10_blk",
        "view:prod_trail_gtx",
    ]


async def test_telegram_button_becomes_structured_action_and_is_deduplicated(
    gateway: Any, fake_agent: FakeAgent, fake_bot: FakeBot
) -> None:
    async with gateway() as gw:
        await post_update(gw, tg_button(200, "add:sku_trail_gtx_10_blk"))
        await post_update(gw, tg_button(200, "add:sku_trail_gtx_10_blk"))  # Telegram re-delivery
    assert len(fake_agent.turns) == 1
    assert fake_agent.turns[0][1]["action"] == {
        "type": "add_to_cart",
        "sku_id": "sku_trail_gtx_10_blk",
        "quantity": 1,
    }
    assert ("answer", {"id": "cb200"}) in fake_bot.sent


async def test_telegram_quote_and_pay_button_never_reach_the_agent(
    gateway: Any, fake_agent: FakeAgent, fake_bot: FakeBot
) -> None:
    fake_agent.reply = lambda _: httpx.Response(
        200,
        content=turn_stream("Here's your summary", [{"type": "quote", "quote": QUOTE, "confirmation": None}]),
        headers={"content-type": "text/event-stream"},
    )
    async with gateway() as gw:
        await post_update(gw, tg_message(300, "checkout"))
        summary = next(p for k, p in fake_bot.sent if k == "message" and p["text"].startswith("🧾"))
        assert "Total: $139.64" in summary["text"]
        assert summary["reply_markup"].inline_keyboard[0][0].callback_data == "pay"
        turns_before = len(fake_agent.turns)
        await post_update(gw, tg_button(301, "pay"))
    assert len(fake_agent.turns) == turns_before  # the confirmation press does not go to the agent


def test_sse_frame_helper() -> None:
    assert parse(sse_event("a", {"x": 1}).decode()) == [("a", {"x": 1})]
