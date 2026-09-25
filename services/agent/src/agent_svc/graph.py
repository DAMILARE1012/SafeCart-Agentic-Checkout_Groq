"""The agent as an explicit LangGraph StateGraph (docs §5.2).

    START → input_guard ─┬─ blocked ─→ refuse ──────────────────────────────┐
                         └─→ sync_phase ─┬─ UI action ─→ action ─→ tools     │
                                         └─→ agent ◀── observe ◀── tools     │
                                               │                             │
                                               ├── tool_calls ─→ tools       │
                                               ├── error ──────────────────→ render → END
                                               └─→ output_guard ─ ok ──────→ render
                                                         └─ bad amounts → agent (once)

Every branch is deterministic code except the ``agent`` node (the LLM).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from agent_svc.commerce_client import CommerceError
from agent_svc.guardrails import strip_amounts, unknown_amounts
from agent_svc.models import ModelProvider, is_rate_limited
from agent_svc.prompts import system_prompt
from agent_svc.settings import AgentSettings
from agent_svc.state import AgentContext, AgentState, Phase
from agent_svc.tools import ALL_TOOLS, action_tool_call, amounts_in, money_text, tools_for

log = structlog.get_logger("agent.graph")

MAX_TOOL_ROUNDS = 4  # after this many tool rounds the model must answer without tools

REFUSALS = {
    "prompt_injection": (
        "I can't do that. I can only help with shopping here, and prices, discounts and payments "
        "are always set by the store. Want me to find something for you or check for valid promo codes?"
    ),
    "abuse": "I'm here to help with shopping. Let me know what you're looking for.",
}
UNAVAILABLE = {
    "code": "assistant_unavailable",
    "message": "I'm having trouble right now. Your cart is saved, so please try again.",
    "retryable": True,
}
BUSY = {
    "code": "assistant_busy",
    "message": "I'm handling a lot of requests right now. Please try again in a few seconds. "
    "Your cart is saved.",
    "retryable": True,
}

_MARKDOWN = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),  # **bold**
    (re.compile(r"__(.+?)__"), r"\1"),  # __bold__
    (re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])"), r"\1"),  # *italic*
    (re.compile(r"`([^`]+)`"), r"\1"),  # `code`
    (re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.M), ""),  # headings
    (re.compile(r"^[ \t]*[-*][ \t]+", re.M), "• "),  # bullets
]


def plain_text(text: str) -> str:
    """Channels render plain text (no HTML/Markdown injection surface); strip stray Markdown."""
    for pattern, replacement in _MARKDOWN:
        text = pattern.sub(replacement, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content.strip()
    parts = [p.get("text", "") if isinstance(p, dict) else str(p) for p in content]
    return "".join(parts).strip()


def trim_history(messages: Sequence[AnyMessage], max_messages: int) -> list[AnyMessage]:
    """Last N messages, starting at a human turn so no tool result is orphaned from its call."""
    recent = list(messages[-max_messages:])
    for i, message in enumerate(recent):
        if isinstance(message, HumanMessage):
            return recent[i:]
    return recent


def cart_summary(cart: dict[str, Any]) -> str:
    if not cart.get("lines"):
        return "empty"
    lines = ", ".join(
        f"{line['name']}{f' ({v})' if (v := line.get('variant_label')) else ''} ×{line['quantity']} "
        f"[cart_item_id={line['id']}]"
        for line in cart["lines"]
    )
    parts = [f"{cart['item_count']} item(s): {lines}", f"subtotal {money_text(cart['subtotal'])}"]
    parts += [f"discount {d['label']} −{money_text(d['amount'])}" for d in cart.get("discounts", [])]
    if cart.get("discounts") and cart.get("total_after_discounts"):
        parts.append(f"total after discounts {money_text(cart['total_after_discounts'])}")
    if issue := cart.get("promo_issue"):
        parts.append(f"promo issue: {issue['message']}")
    parts.append("shipping address: " + ("set" if cart.get("shipping_destination") else "not set"))
    return "; ".join(parts)


def phase_for_cart(cart: dict[str, Any]) -> Phase:
    return "CART_ACTIVE" if cart.get("item_count", 0) > 0 else "BROWSING"


def final_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order-preserving: keep every product block, only the LAST cart/quote; a quote supersedes the cart."""
    last_cart = max((i for i, b in enumerate(blocks) if b["type"] == "cart"), default=None)
    last_quote = max((i for i, b in enumerate(blocks) if b["type"] == "quote"), default=None)
    out: list[dict[str, Any]] = []
    for i, block in enumerate(blocks):
        if block["type"] == "cart" and (i != last_cart or last_quote is not None):
            continue
        if block["type"] == "quote" and i != last_quote:
            continue
        out.append(block)
    return out


def suggestions_for(phase: str, blocks: list[dict[str, Any]]) -> list[str]:
    types = {b["type"] for b in blocks}
    if "quote" in types:
        return []
    cart = next((b["cart"] for b in blocks if b["type"] == "cart"), None)
    if cart and cart.get("item_count"):
        return ["Checkout"] if cart.get("promo_code") else ["Checkout", "Do you have a promo code?"]
    if "product_list" in types:
        return ["Show me cheaper options", "What's in my cart?"]
    if phase == "BROWSING" and not blocks:
        return ["Show me running shoes", "What are your best sellers?"]
    return []


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def build_graph(
    models: ModelProvider, settings: AgentSettings, checkpointer: BaseCheckpointSaver[Any] | None
) -> CompiledStateGraph[AgentState, AgentContext, AgentState, AgentState]:
    async def input_guard(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        if not settings.guardrail_input_classifier_enabled or state.get("pending_action"):
            return {}  # UI actions carry no free text to screen
        try:
            verdict = await models.screen(state.get("user_text", ""))
        except Exception as exc:
            # Fail open: the real protection is structural (no tool can move money or set prices).
            log.warning("input_guard_unavailable", error=type(exc).__name__)
            return {}
        if not verdict.allowed:
            log.info("input_blocked", category=verdict.category)
            return {"blocked_category": verdict.category}
        return {}

    async def refuse(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        text = REFUSALS.get(state.get("blocked_category") or "", REFUSALS["prompt_injection"])
        return {"final_text": text, "messages": [AIMessage(content=text)]}

    async def sync_phase(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        """Derives the phase from commerce-svc's REAL state, never from the model."""
        ctx = runtime.context
        cart: dict[str, Any] | None = None
        if cart_id := state.get("cart_id"):
            try:
                cart = await ctx.commerce.get_cart(cart_id)
            except CommerceError:
                cart = None
        if cart is None:
            cart = await ctx.commerce.get_or_create_cart(ctx.conversation_id, ctx.currency)

        phase = phase_for_cart(cart)
        quote_id = state.get("last_quote_id")
        if quote_id:
            try:
                quote = await ctx.commerce.get_quote(quote_id)
                if quote.get("valid"):
                    phase = "CHECKOUT_REVIEW"
                else:
                    quote_id = None
            except CommerceError:
                quote_id = None

        return {
            "cart_id": cart["id"],
            "phase": phase,
            "last_quote_id": quote_id,
            "cart_summary": cart_summary(cart),
            "turn_amounts": [*state.get("turn_amounts", []), *amounts_in(cart)],
        }

    async def action(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        """A UI control was used: call the tool directly with exact IDs (no LLM choice involved)."""
        call = action_tool_call(state.get("pending_action") or {}, state["turn_id"])
        if call is None:
            return {"pending_action": None}
        return {"pending_action": None, "messages": [AIMessage(content="", tool_calls=[call])]}

    async def agent(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        ctx = runtime.context
        phase = state.get("phase", "BROWSING")
        tools = tools_for(phase) if state.get("tool_rounds", 0) < MAX_TOOL_ROUNDS else []
        prompt: list[BaseMessage] = [
            SystemMessage(
                system_prompt(
                    settings.agent_prompt_version,
                    ctx,
                    phase=phase,
                    cart_summary=state.get("cart_summary", ""),
                )
            ),
            *trim_history(state.get("messages", []), settings.agent_history_max_messages),
        ]
        if feedback := state.get("guard_feedback"):
            prompt.append(SystemMessage(feedback))
        try:
            response = await models.chat(tools).ainvoke(prompt)
        except Exception as exc:
            busy = is_rate_limited(exc)
            log.error("llm_failed", error=type(exc).__name__, phase=phase, rate_limited=busy)
            return {"turn_error": BUSY if busy else UNAVAILABLE}
        usage: dict[str, Any] = dict(response.usage_metadata or {})
        log.info(
            "llm_response",
            phase=phase,
            model=response.response_metadata.get("model_name"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            tools_offered=[t.name for t in tools],
            tool_calls=[c["name"] for c in response.tool_calls],
        )
        return {"messages": [response], "guard_feedback": None}

    async def observe(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        """Collects tool artifacts (UI blocks, amounts) and advances the phase from their results."""
        messages = state.get("messages", [])
        results: list[ToolMessage] = []
        for message in reversed(messages):
            if not isinstance(message, ToolMessage):
                break
            results.insert(0, message)

        blocks = list(state.get("turn_blocks", []))
        amounts = list(state.get("turn_amounts", []))
        update: dict[str, Any] = {"tool_rounds": state.get("tool_rounds", 0) + 1}
        phase = state.get("phase", "BROWSING")
        quote_id = state.get("last_quote_id")
        for result in results:
            artifact = result.artifact if isinstance(result.artifact, dict) else None
            log.info("tool_result", tool=result.name, ok=artifact is not None)
            if artifact is None:
                continue
            blocks.extend(artifact.get("blocks", []))
            amounts.extend(artifact.get("amounts", []))
            for block in artifact.get("blocks", []):
                if block["type"] == "cart":
                    phase = phase_for_cart(block["cart"])
                    quote_id = None  # any cart change supersedes the open quote
                    update["cart_summary"] = cart_summary(block["cart"])
            if artifact.get("quote_id"):
                quote_id, phase = artifact["quote_id"], "CHECKOUT_REVIEW"
            if artifact.get("clear_quote"):
                quote_id = None
                phase = "CART_ACTIVE" if phase == "CHECKOUT_REVIEW" else phase
        return {
            **update,
            "turn_blocks": blocks,
            "turn_amounts": amounts,
            "phase": phase,
            "last_quote_id": quote_id,
        }

    async def output_guard(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        last = state["messages"][-1]
        text = message_text(last)
        if not settings.guardrail_output_amount_validation_enabled:
            return {"final_text": text}
        unknown = unknown_amounts(text, [*state.get("turn_amounts", []), *state.get("known_amounts", [])])
        if not unknown:
            return {"final_text": text}
        log.warning("output_guard_violation", amounts=unknown, retries=state.get("guard_retries", 0))
        if state.get("guard_retries", 0) < settings.guardrail_max_regenerations:
            return {
                "guard_retries": state.get("guard_retries", 0) + 1,
                "guard_feedback": (
                    f"Your previous draft mentioned {', '.join(unknown)}, which do not appear in tool "
                    "results "
                    "or the cart. Rewrite it using only amounts exactly as given there, or leave amounts out."
                ),
                "messages": [RemoveMessage(id=str(last.id))],
            }
        safe = strip_amounts(text, unknown)
        return {"final_text": safe, "messages": [RemoveMessage(id=str(last.id)), AIMessage(content=safe)]}

    async def render(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
        ctx = runtime.context
        if error := state.get("turn_error"):
            # Roll this turn out of the conversation memory so a retry starts clean
            # (side effects already made are replayed via deterministic idempotency keys).
            messages = state.get("messages", [])
            start = next((i for i, m in enumerate(messages) if m.id == ctx.client_message_id), len(messages))
            return {
                "turn_output": {"error": error},
                "messages": [RemoveMessage(id=str(m.id)) for m in messages[start:]],
            }

        blocks = final_blocks(state.get("turn_blocks", []))
        text = plain_text(state.get("final_text") or "") or ("Here you go." if blocks else "")
        suggestions = suggestions_for(state.get("phase", "BROWSING"), blocks)
        now = datetime.now(UTC).isoformat()
        output = {
            "message_id": state["assistant_message_id"],
            "text": text,
            "blocks": blocks,
            "suggestions": suggestions,
        }
        return {
            "turn_output": output,
            "transcript": [
                {
                    "id": ctx.client_message_id,
                    "role": "user",
                    "text": state.get("user_text", ""),
                    "blocks": [],
                    "created_at": now,
                },
                {
                    "id": state["assistant_message_id"],
                    "reply_to": ctx.client_message_id,
                    "role": "assistant",
                    "text": text,
                    "blocks": blocks,
                    "suggestions": suggestions,
                    "created_at": now,
                },
            ],
            # Amounts the customer has now seen: allowed in later turns without a new tool call.
            "known_amounts": list(dict.fromkeys([*state.get("turn_amounts", []), *amounts_in(blocks)]))[
                -200:
            ],
        }

    # -- routing ---------------------------------------------------------------------------------
    def after_guard(state: AgentState) -> Literal["refuse", "sync_phase"]:
        return "refuse" if state.get("blocked_category") else "sync_phase"

    def after_sync(state: AgentState) -> Literal["action", "agent"]:
        return "action" if state.get("pending_action") else "agent"

    def after_action(state: AgentState) -> Literal["tools", "agent"]:
        last = state["messages"][-1] if state.get("messages") else None
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "agent"

    def after_agent(state: AgentState) -> Literal["render", "tools", "output_guard"]:
        if state.get("turn_error"):
            return "render"
        last = state["messages"][-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "output_guard"

    def after_output_guard(state: AgentState) -> Literal["render", "agent"]:
        return "render" if state.get("final_text") else "agent"

    graph = StateGraph(AgentState, context_schema=AgentContext)
    graph.add_node("input_guard", input_guard)
    graph.add_node("refuse", refuse)
    graph.add_node("sync_phase", sync_phase)
    graph.add_node("action", action)
    graph.add_node("agent", agent)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_node("observe", observe)
    graph.add_node("output_guard", output_guard)
    graph.add_node("render", render)

    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges("input_guard", after_guard)
    graph.add_edge("refuse", "render")
    graph.add_conditional_edges("sync_phase", after_sync)
    graph.add_conditional_edges("action", after_action)
    graph.add_conditional_edges("agent", after_agent)
    graph.add_edge("tools", "observe")
    graph.add_edge("observe", "agent")
    graph.add_conditional_edges("output_guard", after_output_guard)
    graph.add_edge("render", END)
    return graph.compile(checkpointer=checkpointer)


def turn_input(
    *,
    user_text: str,
    action: dict[str, Any] | None,
    client_message_id: str,
    turn_id: str,
    assistant_message_id: str,
) -> dict[str, Any]:
    """Graph input for one turn: the new human message + a reset of every per-turn field."""
    content = user_text
    if action:
        content = f"{user_text}\n[UI action: {json.dumps(action, sort_keys=True)}]"
    return {
        "messages": [HumanMessage(content=content, id=client_message_id)],
        "user_text": user_text,
        "pending_action": action,
        "turn_id": turn_id,
        "assistant_message_id": assistant_message_id,
        "turn_blocks": [],
        "turn_amounts": [],
        "tool_rounds": 0,
        "guard_retries": 0,
        "guard_feedback": None,
        "blocked_category": None,
        "final_text": "",
        "turn_error": None,
        "turn_output": None,
    }
