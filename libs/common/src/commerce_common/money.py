"""Money as integer minor units + ISO-4217 currency (design principle P6).

Rules enforced here, once, for every service:
* amounts are ``int`` minor units: never floats;
* arithmetic only between identical currencies;
* every rounding is explicit ROUND_HALF_UP on the target currency's exponent;
* splitting an amount (discount allocation, refunds per line) never creates or
  loses a minor unit (largest-remainder allocation).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

# ISO 4217 minor-unit exponents that differ from the default of 2.
_EXPONENT_OVERRIDES: dict[str, int] = {
    # zero-decimal
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0, "PYG": 0,
    "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    # three-decimal
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}  # fmt: skip
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
BPS_DENOMINATOR = 10_000  # basis points: 825 bps == 8.25 %


class MoneyError(ValueError):
    """Invalid money operation (bad currency, currency mismatch, bad allocation)."""


def validate_currency(currency: str) -> str:
    if not _CURRENCY_RE.fullmatch(currency):
        raise MoneyError(f"invalid ISO-4217 currency code: {currency!r}")
    return currency


def currency_exponent(currency: str) -> int:
    return _EXPONENT_OVERRIDES.get(validate_currency(currency), 2)


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True, order=False)
class Money:
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise MoneyError("amount_minor must be an int")
        validate_currency(self.currency)

    # -- construction -------------------------------------------------------
    @classmethod
    def zero(cls, currency: str) -> Self:
        return cls(0, currency)

    @classmethod
    def from_decimal(cls, major_units: Decimal, currency: str) -> Self:
        """Major units (e.g. Decimal('19.99')) → minor units, rounded half-up."""
        scale = Decimal(10) ** currency_exponent(currency)
        return cls(_round_half_up(major_units * scale), currency)

    def to_decimal(self) -> Decimal:
        return Decimal(self.amount_minor) / (Decimal(10) ** currency_exponent(self.currency))

    # -- arithmetic -----------------------------------------------------------
    def _same(self, other: Money) -> None:
        if not isinstance(other, Money):
            raise MoneyError(f"cannot combine Money with {type(other).__name__}")
        if other.currency != self.currency:
            raise MoneyError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def times(self, quantity: int) -> Money:
        return Money(self.amount_minor * quantity, self.currency)

    def min(self, other: Money) -> Money:
        self._same(other)
        return self if self.amount_minor <= other.amount_minor else other

    # -- comparison -----------------------------------------------------------
    def __lt__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._same(other)
        return self.amount_minor <= other.amount_minor

    @property
    def is_zero(self) -> bool:
        return self.amount_minor == 0

    @property
    def is_negative(self) -> bool:
        return self.amount_minor < 0

    # -- percentages, tax, FX ---------------------------------------------------
    def basis_points(self, bps: int) -> Money:
        """``bps``/10 000 of this amount, rounded half-up (825 → 8.25 %)."""
        return Money(_round_half_up(Decimal(self.amount_minor) * bps / BPS_DENOMINATOR), self.currency)

    def inclusive_tax_portion(self, bps: int) -> Money:
        """Tax contained in a tax-INCLUSIVE amount: amount × r / (1 + r), half-up."""
        value = Decimal(self.amount_minor) * bps / (BPS_DENOMINATOR + bps)
        return Money(_round_half_up(value), self.currency)

    def convert(self, rate: Decimal, to_currency: str) -> Money:
        """Convert with an explicit rate (1 unit of self.currency = ``rate`` units of target)."""
        if rate <= 0:
            raise MoneyError("FX rate must be positive")
        return Money.from_decimal(self.to_decimal() * rate, to_currency)

    # -- allocation -----------------------------------------------------------
    def allocate(self, weights: Sequence[int]) -> list[Money]:
        """Split proportionally to ``weights`` so the parts sum EXACTLY to self.

        Largest-remainder method; ties go to the earlier index (deterministic).
        """
        if not weights:
            raise MoneyError("allocate needs at least one weight")
        if any(w < 0 for w in weights):
            raise MoneyError("weights must be non-negative")
        if self.amount_minor < 0:
            raise MoneyError("cannot allocate a negative amount")
        total_weight = sum(weights)
        if total_weight == 0:
            raise MoneyError("weights must not all be zero")

        raw = [self.amount_minor * w for w in weights]
        parts = [r // total_weight for r in raw]
        leftover = self.amount_minor - sum(parts)
        order = sorted(range(len(weights)), key=lambda i: (-(raw[i] % total_weight), i))
        for i in order[:leftover]:
            parts[i] += 1
        return [Money(p, self.currency) for p in parts]

    def __str__(self) -> str:
        return f"{self.to_decimal():.{currency_exponent(self.currency)}f} {self.currency}"


def sum_money(items: Iterable[Money], currency: str) -> Money:
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total


class MoneyDTO(BaseModel):
    """Wire format shared by every API and the widget: ``{amount_minor, currency}``."""

    model_config = ConfigDict(frozen=True)

    amount_minor: int
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @classmethod
    def of(cls, money: Money) -> MoneyDTO:
        return cls(amount_minor=money.amount_minor, currency=money.currency)

    def to_money(self) -> Money:
        return Money(self.amount_minor, self.currency)
