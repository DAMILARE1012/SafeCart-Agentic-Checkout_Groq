"""Consumer-driven contract: the UI blocks, stream events and history agent-svc produces (from REAL tool
results on the real commerce-svc) must match the widget's published schema."""

from __future__ import annotations

import uuid
from typing import Any

from agent_testkit import ScriptedModels, call, parse_sse, run_turn, say, turn_body
from contract_kit import assert_matches_widget


def mid() -> str:
    return f"m_{uuid.uuid4().hex[:12]}"


async def test_blocks_events_and_history_match_the_widget(make_agent: Any) -> None:
    conversation = f"conv_{uuid.uuid4().hex[:12]}"
    models = ScriptedModels(
        [
            call("search_products", {"query": "trail shoes"}),
            say("Here are some trail shoes."),
            call("add_to_cart", {"sku_id": "sku_trail_gtx_10_blk", "quantity": 1}),
            say("Added."),
            call(
                "set_shipping_address",
                {
                    "line1": "1 Main St",
                    "city": "Austin",
                    "region": "TX",
                    "postal_code": "78701",
                    "country": "US",
                },
            ),
            call("prepare_checkout", {}, call_id="call_2"),
            say("Here's your order summary."),
        ]
    )
    turns = [
        turn_body("show me trail shoes", mid()),
        turn_body("add size 10", mid()),
        turn_body("View it", mid(), action={"type": "view_product", "product_id": "prod_trail_gtx"}),
        turn_body("check out, ship to 1 Main St, Austin, TX 78701, US", mid()),
    ]
    models.replies.insert(4, say("Great choice."))  # reply after the UI action's product card
    seen: set[str] = set()
    async with make_agent(models) as agent:
        for body in turns:
            for name, data in await run_turn(agent, conversation, body):
                if name == "block":
                    seen.add(data["block"]["type"])
                    if data["block"]["type"] == "quote":
                        # agent-svc never holds a grant: the gateway attaches it (or null) before the widget
                        data["block"].setdefault("confirmation", None)
                assert_matches_widget("TurnStreamEvent", {"event": name, "data": data})
        history = (await agent.get(f"/v1/conversations/{conversation}/messages")).json()
    assert seen >= {"product_list", "product_card", "cart", "quote"}
    for message in history["messages"]:
        for block in message["blocks"]:
            if block["type"] == "quote":
                block.setdefault("confirmation", None)
        assert_matches_widget("HistoryMessage", message)


def test_parse_sse_is_the_one_under_test() -> None:
    assert parse_sse('event: turn.completed\ndata: {"message_id": "m"}\n\n') == [
        ("turn.completed", {"message_id": "m"})
    ]
