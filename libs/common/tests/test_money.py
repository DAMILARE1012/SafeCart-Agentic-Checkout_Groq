from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from commerce_common.money import Money, MoneyError, currency_exponent, sum_money

CURRENCIES = st.sampled_from(["USD", "EUR", "JPY", "KWD", "NGN", "GBP"])


def test_exponents_follow_iso_4217() -> None:
    assert currency_exponent("USD") == 2
    assert currency_exponent("JPY") == 0
    assert currency_exponent("KWD") == 3


def test_rejects_floats_bools_and_bad_currency() -> None:
    with pytest.raises(MoneyError):
        Money(19.99, "USD")  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        Money(True, "USD")
    with pytest.raises(MoneyError):
        Money(100, "usd")


def test_currency_mismatch_is_an_error() -> None:
    with pytest.raises(MoneyError):
        _ = Money(100, "USD") + Money(100, "EUR")


def test_from_decimal_rounds_half_up_per_currency() -> None:
    assert Money.from_decimal(Decimal("19.995"), "USD") == Money(2000, "USD")
    assert Money.from_decimal(Decimal("1200.5"), "JPY") == Money(1201, "JPY")
    assert Money.from_decimal(Decimal("1.2345"), "KWD") == Money(1235, "KWD")


def test_basis_points_and_inclusive_tax() -> None:
    assert Money(12900, "USD").basis_points(825) == Money(1064, "USD")  # 8.25 % of $129.00
    # £120.00 including 20 % VAT contains £20.00 of VAT
    assert Money(12000, "GBP").inclusive_tax_portion(2000) == Money(2000, "GBP")


def test_convert_between_exponents() -> None:
    # $10.00 at 1 USD = 150.25 JPY → ¥1,503 (half-up)
    assert Money(1000, "USD").convert(Decimal("150.25"), "JPY") == Money(1503, "JPY")


def test_allocate_is_deterministic_on_ties() -> None:
    assert [m.amount_minor for m in Money(100, "USD").allocate([1, 1, 1])] == [34, 33, 33]


@given(
    amount=st.integers(min_value=0, max_value=10**12),
    weights=st.lists(st.integers(min_value=0, max_value=10**6), min_size=1, max_size=30).filter(any),
    currency=CURRENCIES,
)
def test_allocate_never_creates_or_loses_minor_units(amount: int, weights: list[int], currency: str) -> None:
    parts = Money(amount, currency).allocate(weights)
    assert sum_money(parts, currency) == Money(amount, currency)
    assert all(not p.is_negative for p in parts)
    # zero weight → zero share
    assert all(p.is_zero for p, w in zip(parts, weights, strict=True) if w == 0)


@given(
    amount=st.integers(min_value=0, max_value=10**12),
    weights=st.lists(st.integers(min_value=1, max_value=10**6), min_size=1, max_size=30),
)
def test_allocate_parts_are_within_one_unit_of_exact_share(amount: int, weights: list[int]) -> None:
    parts = Money(amount, "USD").allocate(weights)
    total = sum(weights)
    for part, weight in zip(parts, weights, strict=True):
        exact = Decimal(amount) * weight / total
        assert abs(Decimal(part.amount_minor) - exact) < 1


@given(amount=st.integers(min_value=0, max_value=10**12), bps=st.integers(min_value=0, max_value=10_000))
def test_basis_points_stays_within_half_a_unit(amount: int, bps: int) -> None:
    result = Money(amount, "USD").basis_points(bps).amount_minor
    exact = Decimal(amount) * bps / 10_000
    assert abs(Decimal(result) - exact) <= Decimal("0.5")
