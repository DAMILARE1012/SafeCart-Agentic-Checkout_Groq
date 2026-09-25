"""Graph state, runtime context and phase → tool scoping (docs §5.1–5.2)."""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from agent_svc.checkout_reader import CheckoutReader
from agent_svc.commerce_client import CommerceClient

Phase = Literal["BROWSING", "CART_ACTIVE", "CHECKOUT_REVIEW"]
# AWAITING_PAYMENT / POST_PURCHASE arrive with checkout-svc (M3).

_CATALOG = frozenset({"search_products", "get_product", "list_promotions", "get_order_status"})
_CART_EDIT = frozenset(
    {
        "view_cart",
        "update_cart_item",
        "remove_cart_item",
        "apply_promo_code",
        "remove_promo_code",
        "set_shipping_address",
        "prepare_checkout",
    }
)

# The LLM is only ever *shown* the tools for the current phase. Phase is derived from
# commerce-svc's real state (sync_phase), never from the model.
TOOLS_BY_PHASE: dict[Phase, frozenset[str]] = {
    "BROWSING": _CATALOG | {"add_to_cart", "view_cart"},
    "CART_ACTIVE": _CATALOG | _CART_EDIT | {"add_to_cart"},
    "CHECKOUT_REVIEW": _CATALOG | _CART_EDIT | {"add_to_cart", "cancel_checkout"},
}

# Deterministic tool calls for structured UI actions (the user clicked a control; no LLM choice needed).
ACTION_TOOL_PREFIX = "action_"


@dataclass(frozen=True)
class AgentContext:
    """Per-turn runtime context. Never shown to the LLM, never checkpointed."""

    conversation_id: str
    client_message_id: str
    currency: str
    locale: str
    channel: Literal["web", "telegram"]
    merchant_name: str
    commerce: CommerceClient
    checkout: CheckoutReader | None = None


class AgentState(TypedDict, total=False):
    # --- persisted across turns (checkpointed) ---
    messages: Annotated[list[AnyMessage], add_messages]
    transcript: Annotated[list[dict[str, Any]], operator.add]  # what the UI shows (text + blocks)
    phase: Phase
    cart_id: str | None
    last_quote_id: str | None
    known_amounts: list[str]  # amounts the customer has already been shown ("USD:12900")
    cart_summary: str
    # --- per-turn scratch (reset by every turn's input) ---
    turn_id: str
    assistant_message_id: str
    user_text: str
    pending_action: dict[str, Any] | None
    turn_blocks: list[dict[str, Any]]
    turn_amounts: list[str]
    tool_rounds: int
    guard_retries: int
    guard_feedback: str | None
    blocked_category: str | None
    final_text: str
    turn_error: dict[str, Any] | None
    turn_output: dict[str, Any] | None
