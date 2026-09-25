"""LangChain tools: thin adapters over commerce-svc (docs §5.2).

Each tool returns ``(content, artifact)``:
* ``content``: a compact JSON summary for the LLM (prices pre-formatted as "129.00 USD");
* ``artifact``: the full structured UI block(s) + every money amount shown, for the
  renderer and the output amount guard. The LLM never sees or produces block data.

No tool can confirm, charge or refund. Runtime facts (conversation, cart, currency)
come from ``ToolRuntime``, never from LLM-supplied arguments.
"""
# NOTE: no `from __future__ import annotations`. LangChain inspects these signatures at import time.

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, tool
from langgraph.prebuilt import ToolRuntime

from agent_svc.commerce_client import CommerceError
from agent_svc.state import ACTION_TOOL_PREFIX, TOOLS_BY_PHASE, AgentContext, AgentState
from commerce_common.money import Money

Runtime = ToolRuntime[AgentContext, AgentState]
ToolResult = tuple[str, dict[str, Any] | None]

MAX_DESCRIPTION_CHARS = 280


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def money_text(money: dict[str, Any] | None) -> str | None:
    return str(Money(int(money["amount_minor"]), str(money["currency"]))) if money else None


def amounts_in(value: Any) -> list[str]:
    """Every money object inside a payload, as 'CUR:minor' strings (for the output guard)."""
    found: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("amount_minor"), int) and isinstance(value.get("currency"), str):
            found.append(f"{value['currency']}:{value['amount_minor']}")
        for item in value.values():
            found.extend(amounts_in(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(amounts_in(item))
    return found


def _ok(summary: dict[str, Any], blocks: list[dict[str, Any]], **extra: Any) -> ToolResult:
    amounts = amounts_in(blocks)
    return json.dumps(summary, ensure_ascii=False), {"blocks": blocks, "amounts": amounts, **extra}


def _error(exc: CommerceError) -> ToolResult:
    payload = {"error": exc.code, "message": exc.message}
    if exc.details is not None:
        payload["details"] = exc.details
    return json.dumps(payload), None


def _not_allowed(name: str, runtime: Runtime) -> ToolResult | None:
    """Defense in depth: the model is only shown phase-appropriate tools; reject anything else."""
    if (runtime.tool_call_id or "").startswith(ACTION_TOOL_PREFIX):
        return None  # deterministic call from a UI action
    phase = runtime.state.get("phase", "BROWSING")
    if name not in TOOLS_BY_PHASE[phase]:
        return json.dumps(
            {"error": "tool_not_available", "message": f"{name} is not available right now"}
        ), None
    return None


def _idempotency_key(runtime: Runtime, name: str, args: dict[str, Any]) -> str:
    """Deterministic per (turn, tool, args): a retried turn replays instead of repeating side effects."""
    raw = f"{runtime.context.client_message_id}|{name}|{json.dumps(args, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:48]


_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_UI_ACTION_SUFFIX = re.compile(r"\n\[UI action: .*\]$", re.S)


def _normalise(text: str) -> str:
    return " " + _NON_ALNUM.sub(" ", text.lower()).strip() + " "


def customer_text(runtime: Runtime) -> str:
    """Everything the CUSTOMER typed in this conversation (never model or tool output)."""
    parts = [
        _UI_ACTION_SUFFIX.sub("", str(m.content))
        for m in runtime.state.get("messages", [])
        if isinstance(m, HumanMessage)
    ]
    parts.append(runtime.state.get("user_text", ""))
    return _normalise(" ".join(parts))


def ungrounded_address_fields(runtime: Runtime, *, line1: str, city: str, postal_code: str) -> list[str]:
    """Which address fields the model did NOT take from the customer's own words.

    Deterministic guard against hallucinated tool arguments: a shipping address decides tax, shipping
    and where goods go, so it must come from the customer, never be invented or "assumed".
    """
    said = customer_text(runtime)
    missing = []
    if _normalise(postal_code).replace(" ", "") not in said.replace(" ", ""):
        missing.append("postal_code")
    if _normalise(city) not in said:
        missing.append("city")
    street_words = [w for w in _normalise(line1).split() if len(w) > 1 or w.isdigit()]
    number = next((w for w in street_words if w.isdigit()), None)
    named = [
        w
        for w in street_words
        if not w.isdigit() and w not in {"st", "street", "rd", "road", "ave", "avenue"}
    ]
    if (number and f" {number} " not in said) or (named and not any(f" {w} " in said for w in named)):
        missing.append("line1")
    return missing


def _cart_id(runtime: Runtime) -> str:
    cart_id = runtime.state.get("cart_id")
    if not cart_id:
        raise RuntimeError("cart_id missing from state (sync_phase must run first)")
    return cart_id


def _product_view(p: dict[str, Any], *, with_description: bool = True) -> dict[str, Any]:
    """Compact on purpose: every token here is re-sent on later turns (and counts against rate limits)."""
    description = (p.get("description") or "")[:MAX_DESCRIPTION_CHARS] if with_description else ""
    return {
        "product_id": p["id"],
        "sku_id": p["sku_id"],
        "name": p["name"],
        "variant": p.get("variant_label"),
        "price": money_text(p["price"]),
        "compare_at_price": money_text(p.get("compare_at_price")),
        "in_stock": p["in_stock"],
        "rating": p.get("rating"),
        # Catalog text is untrusted: fenced so the model treats it as data, not instructions.
        **({"description": f"<untrusted>{description}</untrusted>"} if description else {}),
        "variants": [
            {"sku_id": v["sku_id"], "variant": v["variant_label"], "in_stock": v["in_stock"]}
            for v in p.get("variants", [])
        ],
    }


def cart_view(cart: dict[str, Any]) -> dict[str, Any]:
    return {
        "cart_item_count": cart["item_count"],
        "lines": [
            {
                "cart_item_id": line["id"],
                "sku_id": line["sku_id"],
                "name": line["name"],
                "variant": line.get("variant_label"),
                "quantity": line["quantity"],
                "line_total": money_text(line["line_total"]),
            }
            for line in cart["lines"]
        ],
        "subtotal": money_text(cart["subtotal"]),
        "total_after_discounts": money_text(cart.get("total_after_discounts")),
        "discounts": [
            {"label": d["label"], "code": d.get("code"), "amount": money_text(d["amount"])}
            for d in cart["discounts"]
        ],
        "promo_code": cart.get("promo_code"),
        "promo_issue": (cart.get("promo_issue") or {}).get("message"),
        "shipping_address_set": cart.get("shipping_destination") is not None,
    }


def _cart_result(cart: dict[str, Any], **extra: Any) -> ToolResult:
    return _ok(cart_view(cart), [{"type": "cart", "cart": cart}], **extra)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
@tool(response_format="content_and_artifact", parse_docstring=True)
async def search_products(
    query: str, runtime: Runtime, max_price: float | None = None, in_stock_only: bool = False
) -> ToolResult:
    """Search the catalog. Use for any request to find, browse or compare products.

    Args:
        query: What the customer is looking for, in a few keywords (e.g. "waterproof trail shoes").
        max_price: Optional upper price limit in the customer's currency (major units, e.g. 120).
        in_stock_only: Only return products that are in stock.
    """
    if (denied := _not_allowed("search_products", runtime)) is not None:
        return denied
    ctx = runtime.context
    max_minor = None
    if max_price is not None:
        try:
            max_minor = Money.from_decimal(Decimal(str(max_price)), ctx.currency).amount_minor
        except (InvalidOperation, ValueError):
            max_minor = None
    try:
        result = await ctx.commerce.search(
            query, currency=ctx.currency, max_price_minor=max_minor, in_stock_only=in_stock_only
        )
    except CommerceError as exc:
        return _error(exc)
    products = result["products"]
    if not products:
        return json.dumps({"products": [], "note": "No matching products."}), {"blocks": [], "amounts": []}
    return _ok(
        {
            "products": [_product_view(p, with_description=False) for p in products],
            "note": "Descriptions omitted; call get_product for details.",
        },
        [{"type": "product_list", "products": products}],
    )


@tool(response_format="content_and_artifact", parse_docstring=True)
async def get_product(product_id: str, runtime: Runtime) -> ToolResult:
    """Get full details and all variants (sizes/colours) of one product.

    Args:
        product_id: The product_id from a previous search result.
    """
    if (denied := _not_allowed("get_product", runtime)) is not None:
        return denied
    try:
        product = await runtime.context.commerce.get_product(product_id, currency=runtime.context.currency)
    except CommerceError as exc:
        return _error(exc)
    return _ok(_product_view(product), [{"type": "product_card", "product": product}])


@tool(response_format="content_and_artifact")
async def list_promotions(runtime: Runtime) -> ToolResult:
    """List the store's current public promotions and promo codes."""
    if (denied := _not_allowed("list_promotions", runtime)) is not None:
        return denied
    try:
        result = await runtime.context.commerce.promotions()
    except CommerceError as exc:
        return _error(exc)
    return json.dumps(result, ensure_ascii=False), {"blocks": [], "amounts": amounts_in(result)}


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------
@tool(response_format="content_and_artifact", parse_docstring=True)
async def add_to_cart(sku_id: str, runtime: Runtime, quantity: int = 1) -> ToolResult:
    """Add a specific product variant to the customer's cart.

    Args:
        sku_id: The exact sku_id of the chosen variant (from search or product details).
        quantity: How many to add (default 1).
    """
    if (denied := _not_allowed("add_to_cart", runtime)) is not None:
        return denied
    if quantity < 1:
        return json.dumps({"error": "invalid_quantity", "message": "Quantity must be at least 1"}), None
    try:
        cart = await runtime.context.commerce.add_item(
            _cart_id(runtime),
            sku_id,
            quantity,
            idempotency_key=_idempotency_key(
                runtime, "add_to_cart", {"sku_id": sku_id, "quantity": quantity}
            ),
        )
    except CommerceError as exc:
        return _error(exc)
    return _cart_result(cart)


@tool(response_format="content_and_artifact")
async def view_cart(runtime: Runtime) -> ToolResult:
    """Show the customer's current cart (items, subtotal, discounts)."""
    if (denied := _not_allowed("view_cart", runtime)) is not None:
        return denied
    try:
        cart = await runtime.context.commerce.get_cart(_cart_id(runtime))
    except CommerceError as exc:
        return _error(exc)
    return _cart_result(cart)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def update_cart_item(cart_item_id: str, quantity: int, runtime: Runtime) -> ToolResult:
    """Change the quantity of a line in the cart (0 removes it).

    Args:
        cart_item_id: The cart_item_id of the line (from the cart).
        quantity: The new total quantity for that line.
    """
    if (denied := _not_allowed("update_cart_item", runtime)) is not None:
        return denied
    try:
        cart = await runtime.context.commerce.set_quantity(_cart_id(runtime), cart_item_id, max(quantity, 0))
    except CommerceError as exc:
        return _error(exc)
    return _cart_result(cart)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def remove_cart_item(cart_item_id: str, runtime: Runtime) -> ToolResult:
    """Remove a line from the cart.

    Args:
        cart_item_id: The cart_item_id of the line to remove.
    """
    if (denied := _not_allowed("remove_cart_item", runtime)) is not None:
        return denied
    try:
        cart = await runtime.context.commerce.remove_item(_cart_id(runtime), cart_item_id)
    except CommerceError as exc:
        return _error(exc)
    return _cart_result(cart)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def apply_promo_code(code: str, runtime: Runtime) -> ToolResult:
    """Apply a promo code the customer provided. The store validates it; never invent codes.

    Args:
        code: The promo code exactly as the customer gave it.
    """
    if (denied := _not_allowed("apply_promo_code", runtime)) is not None:
        return denied
    try:
        cart = await runtime.context.commerce.apply_promo(_cart_id(runtime), code)
    except CommerceError as exc:
        return _error(exc)
    return _cart_result(cart)


@tool(response_format="content_and_artifact")
async def remove_promo_code(runtime: Runtime) -> ToolResult:
    """Remove the promo code from the cart."""
    if (denied := _not_allowed("remove_promo_code", runtime)) is not None:
        return denied
    try:
        cart = await runtime.context.commerce.remove_promo(_cart_id(runtime))
    except CommerceError as exc:
        return _error(exc)
    return _cart_result(cart)


@tool(response_format="content_and_artifact", parse_docstring=True)
async def set_shipping_address(
    line1: str,
    city: str,
    postal_code: str,
    country: str,
    runtime: Runtime,
    region: str | None = None,
    line2: str | None = None,
    name: str | None = None,
) -> ToolResult:
    """Save the customer's shipping address (needed to calculate tax and shipping at checkout).

    Args:
        line1: Street address.
        city: City.
        postal_code: Postal or ZIP code.
        country: Two-letter ISO country code, e.g. US, GB, NG.
        region: State/province code if applicable, e.g. TX.
        line2: Apartment, suite, etc.
        name: Recipient name.
    """
    if (denied := _not_allowed("set_shipping_address", runtime)) is not None:
        return denied
    if missing := ungrounded_address_fields(runtime, line1=line1, city=city, postal_code=postal_code):
        return json.dumps(
            {
                "error": "address_not_from_customer",
                "message": "Ask the customer for their full shipping address. Never invent or assume one.",
                "fields_not_provided_by_customer": missing,
            }
        ), None
    address = {
        "line1": line1,
        "line2": line2,
        "city": city,
        "region": region.upper() if region else None,
        "postal_code": postal_code,
        "country": country.strip().upper(),
        "name": name,
    }
    try:
        cart = await runtime.context.commerce.set_shipping_address(_cart_id(runtime), address)
    except CommerceError as exc:
        return _error(exc)
    return _ok({"shipping_address_saved": True, "destination": cart.get("shipping_destination")}, [])


# ---------------------------------------------------------------------------
# Checkout (quote only: payment requires the customer's explicit confirmation, outside the LLM)
# ---------------------------------------------------------------------------
@tool(response_format="content_and_artifact")
async def prepare_checkout(runtime: Runtime) -> ToolResult:
    """Create the binding order summary (items, discounts, shipping, tax, total) for the customer
    to review. This does NOT charge anything: the customer confirms by pressing "Confirm & pay"."""
    if (denied := _not_allowed("prepare_checkout", runtime)) is not None:
        return denied
    cart_id = _cart_id(runtime)
    try:
        quote = await runtime.context.commerce.create_quote(
            cart_id, idempotency_key=_idempotency_key(runtime, "prepare_checkout", {"cart_id": cart_id})
        )
    except CommerceError as exc:
        return _error(exc)
    summary = {
        "order_summary_created": True,
        "total": money_text(quote["total"]),
        "subtotal": money_text(quote["subtotal"]),
        "shipping": money_text(quote["shipping"]),
        "tax": money_text(quote["tax_total"]),
        "tax_included_in_prices": quote["tax_inclusive"],
        "expires_at": quote["expires_at"],
        "next_step": "Customer reviews the summary card and presses Confirm & pay. Do not repeat every line.",
    }
    # The gateway attaches the single-use confirmation grant to this block; the agent never holds it.
    return _ok(summary, [{"type": "quote", "quote": quote, "confirmation": None}], quote_id=quote["id"])


@tool(response_format="content_and_artifact")
async def cancel_checkout(runtime: Runtime) -> ToolResult:
    """Discard the current order summary so the customer can keep shopping."""
    if (denied := _not_allowed("cancel_checkout", runtime)) is not None:
        return denied
    return json.dumps({"checkout_cancelled": True}), {"blocks": [], "amounts": [], "clear_quote": True}


@tool(response_format="content_and_artifact")
async def get_order_status(runtime: Runtime) -> ToolResult:
    """Look up the status of the customer's most recent order (payment, preparation, refund)."""
    if (denied := _not_allowed("get_order_status", runtime)) is not None:
        return denied
    reader = runtime.context.checkout
    if reader is None:
        return json.dumps(
            {"error": "order_tracking_unavailable", "message": "Order tracking is unavailable"}
        ), None
    try:
        order = await reader.latest_order(runtime.context.conversation_id)
    except httpx.HTTPError:
        return json.dumps(
            {"error": "order_tracking_unavailable", "message": "Please try again shortly"}
        ), None
    if order is None:
        return json.dumps({"orders": [], "note": "No orders in this conversation yet."}), {
            "blocks": [],
            "amounts": [],
        }
    summary = {"order_id": order["id"], "status": order["status"], "total": money_text(order["total"])}
    return _ok(summary, [{"type": "order_status", "order": order}])


ALL_TOOLS: list[BaseTool] = [
    search_products,
    get_product,
    list_promotions,
    add_to_cart,
    view_cart,
    update_cart_item,
    remove_cart_item,
    apply_promo_code,
    remove_promo_code,
    set_shipping_address,
    prepare_checkout,
    cancel_checkout,
    get_order_status,
]
TOOLS_BY_NAME: dict[str, BaseTool] = {t.name: t for t in ALL_TOOLS}


def tools_for(phase: str) -> list[BaseTool]:
    allowed = TOOLS_BY_PHASE.get(phase, TOOLS_BY_PHASE["BROWSING"])  # type: ignore[call-overload]
    return [t for t in ALL_TOOLS if t.name in allowed]


# Structured UI actions → deterministic tool calls
ACTION_TOOLS: dict[str, str] = {
    "add_to_cart": "add_to_cart",
    "update_cart_item": "update_cart_item",
    "remove_cart_item": "remove_cart_item",
    "view_product": "get_product",
    "start_checkout": "prepare_checkout",
    "refresh_quote": "prepare_checkout",
}


def action_tool_call(action: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    name = ACTION_TOOLS.get(str(action.get("type")))
    if name is None:
        return None
    args = {k: v for k, v in action.items() if k != "type"}
    if name == "get_product":
        args = {"product_id": action.get("product_id")}
    if name == "prepare_checkout":
        args = {}
    return {"name": name, "args": args, "id": f"{ACTION_TOOL_PREFIX}{turn_id}", "type": "tool_call"}
