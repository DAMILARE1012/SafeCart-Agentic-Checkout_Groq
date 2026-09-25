"""Telegram channel (python-telegram-bot): maps updates to agent turns and renders UI blocks natively.

* Text messages → agent turns; button presses (callback_query) → structured actions with exact IDs.
* Conversation per chat: ``tg_<chat_id>``. Updates are de-duplicated by ``update_id``.
* Everything is sent as plain text (no parse mode), so catalog or model text can't inject markup.
* Payment confirmation buttons are handled here, never by the agent (docs §6.2); they open in M3.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import structlog
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError

from commerce_common.money import Money, currency_exponent
from gateway_svc.agent_client import AgentClient, TurnResult, UpstreamError
from gateway_svc.limits import Deduper, RateLimiter
from gateway_svc.settings import GatewaySettings

log = structlog.get_logger("gateway.telegram")

MAX_PRODUCTS = 5
DEDUPE_TTL_S = 24 * 3600
_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "NGN": "₦"}


class BotApi(Protocol):
    """The subset of telegram.Bot we use (lets tests substitute a recorder)."""

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> Any: ...
    async def send_photo(self, chat_id: int, photo: str, **kwargs: Any) -> Any: ...
    async def send_chat_action(self, chat_id: int, action: str, **kwargs: Any) -> Any: ...
    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...


def fmt(money: dict[str, Any] | None) -> str:
    if not money:
        return ""
    m = Money(int(money["amount_minor"]), str(money["currency"]))
    symbol = _SYMBOLS.get(m.currency)
    number = f"{m.to_decimal():,.{currency_exponent(m.currency)}f}"
    return f"{symbol}{number}" if symbol else f"{number} {m.currency}"


def product_caption(p: dict[str, Any], *, full: bool = False) -> str:
    lines = [p["name"] + (f" ({p['variant_label']})" if p.get("variant_label") else "")]
    price = fmt(p["price"])
    if (compare := p.get("compare_at_price")) and compare["amount_minor"] > p["price"]["amount_minor"]:
        price += f"  (was {fmt(compare)})"
    lines.append(price if p.get("in_stock") else f"{price} · out of stock")
    if full and p.get("description"):
        lines.append(p["description"])
    return "\n".join(lines)


def product_keyboard(p: dict[str, Any], *, details: bool = True) -> InlineKeyboardMarkup:
    row = []
    if p.get("in_stock"):
        row.append(InlineKeyboardButton("🛒 Add to cart", callback_data=f"add:{p['sku_id']}"))
    if details:
        row.append(InlineKeyboardButton("Details", callback_data=f"view:{p['id']}"))
    return InlineKeyboardMarkup([row] if row else [])


def cart_text(cart: dict[str, Any]) -> str:
    if not cart.get("lines"):
        return "🛒 Your cart is empty."
    lines = ["🛒 Your cart"]
    for line in cart["lines"]:
        variant = f" ({line['variant_label']})" if line.get("variant_label") else ""
        lines.append(f"• {line['quantity']}× {line['name']}{variant}: {fmt(line['line_total'])}")
    lines += [f"• {d['label']}: −{fmt(d['amount'])}" for d in cart.get("discounts", [])]
    lines.append(f"Subtotal: {fmt(cart['subtotal'])}")
    return "\n".join(lines)


def quote_text(quote: dict[str, Any]) -> str:
    lines = ["🧾 Order summary"]
    lines += [f"• {line['quantity']}× {line['name']}: {fmt(line['line_total'])}" for line in quote["lines"]]
    lines += [f"• {d['label']}: −{fmt(d['amount'])}" for d in quote.get("discounts", [])]
    shipping = "Free" if quote["shipping"]["amount_minor"] == 0 else fmt(quote["shipping"])
    lines.append(f"Shipping: {shipping}")
    lines.append(f"Tax{' (included)' if quote['tax_inclusive'] else ''}: {fmt(quote['tax_total'])}")
    lines.append(f"Total: {fmt(quote['total'])}")
    return "\n".join(lines)


def suggestion_keyboard(suggestions: list[str]) -> ReplyKeyboardMarkup | None:
    if not suggestions:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton(s)] for s in suggestions[:4]], resize_keyboard=True, one_time_keyboard=True
    )


class TelegramChannel:
    def __init__(
        self,
        bot: BotApi,
        agent: AgentClient,
        settings: GatewaySettings,
        *,
        deduper: Deduper,
        limiter: RateLimiter,
    ) -> None:
        self._bot = bot
        self._agent = agent
        self._settings = settings
        self._deduper = deduper
        self._limiter = limiter
        self._tasks: set[asyncio.Task[None]] = set()

    # -- entry points ------------------------------------------------------------------------
    def submit(self, update: Update) -> None:
        """Webhook path: acknowledge Telegram immediately, process in the background."""
        task = asyncio.create_task(self.handle(update))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def handle(self, update: Update) -> None:
        if not await self._deduper.first_time(f"tg-update:{update.update_id}", DEDUPE_TTL_S):
            return  # Telegram re-delivered an update we already processed
        try:
            if update.callback_query is not None:
                await self._on_button(update)
            elif update.message is not None and update.message.text:
                await self._on_text(update)
        except Exception:
            log.exception("telegram_update_failed", update_id=update.update_id)

    # -- handlers ------------------------------------------------------------------------------
    async def _on_text(self, update: Update) -> None:
        message = update.message
        assert message is not None
        text = (message.text or "").strip()
        if text.startswith("/start"):
            text = "Hi!"
        locale = (
            message.from_user.language_code if message.from_user and message.from_user.language_code else "en"
        )
        await self._turn(message.chat.id, update.update_id, text[:1000], None, locale)

    async def _on_button(self, update: Update) -> None:
        query = update.callback_query
        assert query is not None
        await self._bot.answer_callback_query(query.id)
        chat_id = query.message.chat.id if query.message else None
        if chat_id is None or not query.data:
            return
        kind, _, value = query.data.partition(":")
        action: dict[str, Any] | None
        if kind == "add":
            action, text = {"type": "add_to_cart", "sku_id": value, "quantity": 1}, "Add this to my cart"
        elif kind == "view":
            action, text = {"type": "view_product", "product_id": value}, "Tell me more about this"
        elif kind == "co":
            action, text = {"type": "start_checkout"}, "I'm ready to check out"
        elif kind == "pay":
            # Explicit payment confirmation is a gateway→checkout-svc path, never the agent (M3).
            await self._bot.send_message(
                chat_id, "Secure payment opens with our next release. Your cart is saved!"
            )
            return
        else:
            return
        locale = query.from_user.language_code or "en"
        await self._turn(chat_id, update.update_id, text, action, locale)

    async def _turn(
        self, chat_id: int, update_id: int, text: str, action: dict[str, Any] | None, locale: str
    ) -> None:
        if not await self._limiter.allow(f"tg:{chat_id}", self._settings.rate_limit_messages_per_minute, 60):
            await self._bot.send_message(chat_id, "You're sending messages quickly. Give me a moment 🙂")
            return
        await self._bot.send_chat_action(chat_id, ChatAction.TYPING)
        payload = {
            "client_message_id": f"tg_{update_id}",
            "text": text,
            "action": action,
            "context": {
                "currency": self._settings.default_currency,
                "locale": locale,
                "channel": "telegram",
                "merchant_name": self._settings.merchant_name,
            },
        }
        try:
            result = await self._agent.collect_turn(f"tg_{chat_id}", payload)
        except UpstreamError as exc:
            message = (
                "Still working on your previous message…" if exc.code == "turn_in_progress" else exc.message
            )
            await self._bot.send_message(chat_id, message)
            return
        await self.render(chat_id, result)

    # -- rendering -----------------------------------------------------------------------------
    async def render(self, chat_id: int, result: TurnResult) -> None:
        if result.error:
            await self._bot.send_message(chat_id, str(result.error.get("message", "Something went wrong.")))
            return
        keyboard = suggestion_keyboard(result.suggestions)
        if result.text:
            await self._bot.send_message(chat_id, result.text, reply_markup=keyboard)
        for block in result.blocks:
            await self._render_block(chat_id, block)

    async def _render_block(self, chat_id: int, block: dict[str, Any]) -> None:
        kind = block["type"]
        if kind == "product_list":
            for product in block["products"][:MAX_PRODUCTS]:
                await self._send_product(chat_id, product, full=False)
        elif kind == "product_card":
            await self._send_product(chat_id, block["product"], full=True)
        elif kind == "cart":
            cart = block["cart"]
            buttons = [[InlineKeyboardButton("✅ Checkout", callback_data="co")]] if cart.get("lines") else []
            await self._bot.send_message(chat_id, cart_text(cart), reply_markup=InlineKeyboardMarkup(buttons))
        elif kind == "quote":
            quote = block["quote"]
            pay = InlineKeyboardButton(f"🔒 Confirm & pay {fmt(quote['total'])}", callback_data="pay")
            await self._bot.send_message(
                chat_id, quote_text(quote), reply_markup=InlineKeyboardMarkup([[pay]])
            )
        elif kind == "notice":
            await self._bot.send_message(chat_id, str(block.get("text", "")))

    async def _send_product(self, chat_id: int, product: dict[str, Any], *, full: bool) -> None:
        caption = product_caption(product, full=full)
        keyboard = product_keyboard(product, details=not full)
        if product.get("image_url"):
            try:
                await self._bot.send_photo(
                    chat_id, product["image_url"], caption=caption[:1024], reply_markup=keyboard
                )
                return
            except TelegramError as exc:
                log.warning("telegram_photo_failed", error=str(exc))
        await self._bot.send_message(chat_id, caption, reply_markup=keyboard)


async def poll_updates(bot: Any, channel: TelegramChannel, allowed_updates: list[str]) -> None:
    """Long-polling loop for local development (no public URL needed)."""
    await bot.delete_webhook(drop_pending_updates=False)
    offset: int | None = None
    backoff = 1.0
    log.info("telegram_polling_started")
    while True:
        try:
            updates = await bot.get_updates(
                offset=offset, timeout=25, read_timeout=35, allowed_updates=allowed_updates
            )
            backoff = 1.0
            for update in updates:
                offset = update.update_id + 1
                await channel.handle(update)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("telegram_poll_failed", error=type(exc).__name__, retry_in_s=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
