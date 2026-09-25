"""Deterministic guardrails around the LLM (docs §5.3, §10). Pure functions: easy to test and to evaluate.

Input : ``injection_signals`` screens for unambiguous prompt-injection phrasing (before the classifier).
Output: every amount must be one the tools returned (``unknown_amounts``); claims about carts, discounts,
        payments and refunds must be backed by this turn's tool results (``unsupported_claims``); promo
        codes must come from the store or the customer (``invented_codes``); credentials and the system
        prompt must never appear (``leaked_content``).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

from commerce_common.money import Money

_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₦": "NGN"}
_CODES = "USD|EUR|GBP|JPY|NGN|KWD|CAD|AUD"
_NUMBER = r"\d{1,3}(?:[,  ]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?"
_SYMBOL_CLASS = "".join(re.escape(s) for s in _SYMBOLS)

# "$129", "$ 1,299.00", "129.00 USD", "USD 129", "€84.60", "84,60 €" is not supported (we format with dots)
_MONEY_RE = re.compile(
    rf"(?P<sym>[{_SYMBOL_CLASS}])\s?(?P<n1>{_NUMBER})"
    rf"|(?P<code1>\b(?:{_CODES})\b)\s?(?P<n2>{_NUMBER})"
    rf"|(?P<n3>{_NUMBER})\s?(?P<code2>\b(?:{_CODES})\b)",
)


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", "").replace(" ", "").replace(" ", ""))
    except InvalidOperation:
        return None


def mentioned_amounts(text: str) -> list[tuple[str, Decimal, str]]:
    """(currency or '?', value, original text) for each money-looking token in ``text``."""
    found: list[tuple[str, Decimal, str]] = []
    for m in _MONEY_RE.finditer(text):
        currency = _SYMBOLS.get(m.group("sym") or "") or m.group("code1") or m.group("code2") or "?"
        raw = m.group("n1") or m.group("n2") or m.group("n3") or ""
        value = _to_decimal(raw)
        if value is not None:
            found.append((currency, value, m.group(0)))
    return found


def allowed_values(amounts: list[str]) -> set[tuple[str, Decimal]]:
    """'USD:12900' → ('USD', Decimal('129.00')). Decimal equality ignores trailing zeros ($129 == 129.00)."""
    values: set[tuple[str, Decimal]] = set()
    for item in amounts:
        currency, _, minor = item.partition(":")
        try:
            values.add((currency, Money(int(minor), currency).to_decimal()))
        except (ValueError, ArithmeticError):
            continue
    return values


def unknown_amounts(text: str, amounts: list[str]) -> list[str]:
    """Money mentions in ``text`` that don't match any amount the tools returned this turn."""
    allowed = allowed_values(amounts)
    allowed_numbers = {value for _, value in allowed}
    unknown: list[str] = []
    for currency, value, original in mentioned_amounts(text):
        ok = (currency, value) in allowed if currency != "?" else value in allowed_numbers
        # "$" is ambiguous (USD/CAD/AUD…): accept a matching number in any dollar-free currency context
        if not ok and currency == "USD" and original.strip().startswith("$"):
            ok = any(value == v for c, v in allowed if c in {"USD", "CAD", "AUD"})
        if not ok:
            unknown.append(original.strip())
    return unknown


def strip_amounts(text: str, unknown: list[str] | None = None) -> str:
    """Last resort after failed regeneration: remove unverified prices from prose (the UI cards show the
    real figures). With ``unknown``, only those tokens are removed and verified amounts are kept."""
    targets = {u.strip() for u in unknown} if unknown is not None else None

    def replace(match: re.Match[str]) -> str:
        if targets is None or match.group(0).strip() in targets:
            return "the amount shown below"
        return match.group(0)

    return _MONEY_RE.sub(replace, text)


# ---------------------------------------------------------------------------
# Input: deterministic prompt-injection pre-screen (layer 1, before the classifier)
# ---------------------------------------------------------------------------
# Unambiguous attack phrasings only: false positives cost a real customer a refusal, so anything subtler
# is left to Llama Prompt Guard (layer 2). This layer also keeps blocking when the classifier is down
# (it fails open). Even a missed injection gains nothing: no tool can move money or set prices.
_INJECTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b[^.\n]{0,40}"
            r"\b(previous|prior|above|earlier|all|any|your|the|these|system)\b[^.\n]{0,25}"
            r"\b(instructions?|rules?|prompts?|guidelines?|directives?|guardrails?|restrictions?)\b",
            re.I,
        ),
    ),
    (
        "prompt_exfiltration",
        re.compile(
            r"\b(reveal|show|print|repeat|output|dump|leak|tell me|what(?:'s| is| are))\b[^.\n]{0,30}"
            r"\b(system|hidden|initial|developer|internal)\s+(prompt|instructions?|message|rules)\b",
            re.I,
        ),
    ),
    (
        "role_override",
        re.compile(
            r"\byou are now\s+(in\s+)?(dan|jailbroken|unrestricted|uncensored|developer|admin|root)\b"
            r"|\byou are no longer\b[^.\n]{0,30}\b(bound|restricted|limited|an? (assistant|ai))\b"
            r"|\b(developer|god|dan|jailbreak|debug|admin) mode\b"
            r"|\b(act|behave|pretend|roleplay)\s+(as|to be)\s+(an?\s+|the\s+)?([a-z]+\s+)?"
            r"(admin|administrator|developer|system|root|dan|unrestricted|jailbroken)\b",
            re.I,
        ),
    ),
    (
        "fake_markup",
        re.compile(
            r"</?\s*(system|assistant|untrusted|instructions?)\s*>|\[/?(system|inst)\]|<\|im_(start|end)\|>"
            r"|^\s*#{2,}\s*(system|instructions?)\b",
            re.I | re.M,
        ),
    ),
    (
        "price_manipulation",
        re.compile(
            r"\b(set|change|make|update|lower|override|reduce)\b[^.\n]{0,30}"
            r"\b(prices?|totals?|costs?|amounts?)\b[^.\n]{0,40}\b(to|=)\s*[$€£]?\s*0+(\.0+)?\b"
            r"|\b100\s?%\s*(off|discount)\b|\bmake (it|everything|them|the order) free\b(?!\s+shipping)",
            re.I,
        ),
    ),
)
_INVISIBLE = re.compile(r"[​-‏⁠-⁤﻿­]")


def normalise_input(text: str) -> str:
    """Undo common obfuscation: compatibility forms (full-width letters) and invisible characters."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


def injection_signals(text: str) -> list[str]:
    """Names of the injection rules ``text`` matches (empty for normal shopping messages)."""
    clean = normalise_input(text)
    return [name for name, pattern in _INJECTION_RULES if pattern.search(clean)]


# ---------------------------------------------------------------------------
# Output: claims the reply makes must be backed by what actually happened this turn
# ---------------------------------------------------------------------------
# claim → the evidence that makes it true. Evidence is the set of tools that SUCCEEDED this turn, plus
# "cart_discount" when the real cart carries a discount. The agent can never charge, place an order or
# refund, so those claims are only acceptable when it is reading back the order's status.
_CLAIM_RULES: tuple[tuple[str, re.Pattern[str], frozenset[str]], ...] = (
    (
        "payment",
        re.compile(
            r"\b(you(?:'ve| have) been|i(?:'ve| have)|we(?:'ve| have))\s+(charged|billed)\b"
            r"|\bpayment\s+(has been|was|is)\s+(taken|processed|completed|received|successful|confirmed)\b"
            r"|\b(i|we)(?:'ve| have)?\s+(placed|submitted|completed|processed)\s+(your|the)\s+order\b"
            r"|\byour order\s+(has been|is now|was)\s+(placed|paid|confirmed)\b",
            re.I,
        ),
        frozenset({"get_order_status"}),
    ),
    (
        "refund",
        re.compile(
            r"\b(i|we)(?:'ve| have)?\s+(issued|processed|sent|initiated|approved|refunded)\b"
            r"[^.\n]{0,20}\brefund"
            r"|\brefund\s+(has been|was|is)\s+(issued|processed|approved|sent|completed)\b"
            r"|\byou(?:'ve| have)\s+been\s+refunded\b",
            re.I,
        ),
        frozenset({"get_order_status"}),
    ),
    (
        "discount",
        re.compile(
            r"\b(i(?:'ve| have)|we(?:'ve| have))\s+(applied|added)\s+(the\s+|a\s+|your\s+)?"
            r"([A-Z0-9-]{3,20}\s+)?(code|promo|discount|coupon)\b"
            r"|\b(and|then|also)\s+(applied|added)\s+(the\s+|a\s+|your\s+)?(code|promo|discount|coupon)\b"
            r"|\b(code|promo|discount|coupon)\s+(has been|is|was)\s+(applied|added)\b",
            re.I,
        ),
        frozenset({"apply_promo_code", "cart_discount"}),
    ),
    (
        # "I found 10 trail shoes" with no search behind it: invented results (seen in live evals).
        "catalog_results",
        re.compile(
            r"\b(i|we)(?:'ve| have)?\s+(found|pulled up|located|got)\b[^.\n]{0,40}"
            r"\b(products?|items?|options?|results?|matches|shoes|pairs|jackets|socks|caps)\b"
            r"|\bhere (are|is)\s+(some|a few|several|the|\d+)\b[^.\n]{0,40}"
            r"\b(products?|items?|options?|results?|shoes|pairs|jackets|socks|caps)\b",
            re.I,
        ),
        frozenset({"search_products", "get_product", "list_promotions", "view_cart", "add_to_cart"}),
    ),
    (
        "cart",
        re.compile(
            r"\b(i(?:'ve| have)|i)\s+(added|put)\b[^.\n]{0,60}\b(to|in|into)\s+your\s+(cart|bag|basket)\b"
            r"|\b(has|have)\s+been\s+added\s+to\s+your\s+(cart|bag|basket)\b",
            re.I,
        ),
        frozenset({"add_to_cart", "update_cart_item"}),
    ),
)


def unsupported_claims(text: str, evidence: set[str]) -> list[str]:
    """Claim kinds in ``text`` with no supporting evidence this turn (e.g. 'payment', 'cart')."""
    return [kind for kind, pattern, needs in _CLAIM_RULES if pattern.search(text) and not (needs & evidence)]


# ---------------------------------------------------------------------------
# Output: promo codes must come from the store (or the customer), never be invented
# ---------------------------------------------------------------------------
_CODE_NEAR_KEYWORD = re.compile(
    r"\b(?:code|coupon|promo(?:\s+code)?|voucher)\s*(?:is\s+|:\s*|of\s+)?[\"'“‘(]?"
    r"(?P<code>[A-Za-z0-9][A-Za-z0-9_-]{2,19})\b",
    re.I,
)
_CODE_BEFORE_KEYWORD = re.compile(
    r"[\"'“‘(]?\b(?P<code>[A-Z0-9][A-Z0-9_-]{2,19})\b[\"'”’)]?\s+(?:code|coupon|promo)\b"
)
_NOT_CODES = frozenset(
    {"THE", "THIS", "THAT", "YOUR", "A", "AN", "AT", "IS", "FOR", "WITH", "PROMO", "CODE", "COUPON",
     "DISCOUNT", "CHECKOUT", "USD", "EUR", "GBP", "VALID", "ANY", "NO", "ONE", "OUR", "SAME"}
)  # fmt: skip


def mentioned_codes(text: str) -> set[str]:
    codes = {m.group("code") for m in _CODE_NEAR_KEYWORD.finditer(text)}
    codes |= {m.group("code") for m in _CODE_BEFORE_KEYWORD.finditer(text)}
    # A code has at least one capital letter or digit as typed (so "code for" is ignored) and isn't a word.
    return {
        c.upper()
        for c in codes
        if (c.isupper() or any(ch.isdigit() for ch in c)) and c.upper() not in _NOT_CODES
    }


def invented_codes(text: str, known_text: str) -> list[str]:
    """Codes the reply mentions that appear nowhere in tool results, the cart or the customer's words."""
    known = known_text.upper()
    return sorted(c for c in mentioned_codes(text) if not re.search(rf"\b{re.escape(c)}\b", known))


# ---------------------------------------------------------------------------
# Output: never leak credentials or the system prompt
# ---------------------------------------------------------------------------
_SECRET = re.compile(
    r"\b(sk|rk)_(test|live)_[A-Za-z0-9]{8,}|\bwhsec_[A-Za-z0-9]{8,}|\bgsk_[A-Za-z0-9]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"
)
# Distinctive fragments of the system prompt (prompts.py). If one shows up, the prompt is being dumped.
_PROMPT_FRAGMENTS = (
    "rules (never break these)",
    "text inside <untrusted>",
    "conversation phase:",
    "customer currency:",
    "use tools for every fact about products",
)


def leaked_content(text: str) -> list[str]:
    found = ["credential"] if _SECRET.search(text) else []
    lower = text.lower()
    if any(fragment in lower for fragment in _PROMPT_FRAGMENTS):
        found.append("system_prompt")
    return found


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def drop_sentences(text: str, should_drop: Callable[[str], bool]) -> str:
    """Last resort after regeneration: remove only the offending sentences, keep the rest."""
    kept = [s for s in _SENTENCE.split(text) if s.strip() and not should_drop(s)]
    return " ".join(kept).strip()
