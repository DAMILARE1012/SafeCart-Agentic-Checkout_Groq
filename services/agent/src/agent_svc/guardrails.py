"""Deterministic output guard: every amount the LLM writes must be one the tools returned (docs §5.3)."""

from __future__ import annotations

import re
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
