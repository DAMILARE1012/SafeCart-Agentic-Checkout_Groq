from agent_svc.guardrails import mentioned_amounts, strip_amounts, unknown_amounts

TURN = ["USD:12900", "USD:12568", "EUR:11900", "NGN:19995000"]


def test_detects_common_money_formats() -> None:
    found = [
        (c, str(v)) for c, v, _ in mentioned_amounts("It's $129, or 129.00 USD, EUR 119.00 and ₦199,950.00")
    ]
    assert ("USD", "129") in found
    assert ("USD", "129.00") in found
    assert ("EUR", "119.00") in found
    assert ("NGN", "199950.00") in found


def test_accepts_amounts_from_tool_results_in_any_format() -> None:
    text = "The Trail Runner is $129 (129.00 USD) and your total is $125.68."
    assert unknown_amounts(text, TURN) == []


def test_flags_invented_or_computed_amounts() -> None:
    text = "It's $129.00, so you save $20.00 and pay $99 in tax."
    assert unknown_amounts(text, TURN) == ["$20.00", "$99"]


def test_currency_must_match() -> None:
    assert unknown_amounts("That's 129.00 EUR", TURN) == ["129.00 EUR"]


def test_ignores_non_money_numbers() -> None:
    assert unknown_amounts("Size 10, 2 pairs, 10% off, rated 4.7", TURN) == []


def test_strip_amounts() -> None:
    assert strip_amounts("You save $20.00 today") == "You save the amount shown below today"


def test_strip_only_unknown_amounts_keeps_verified_ones() -> None:
    text = "Your subtotal is 129.00 USD and you save $20.00."
    unknown = unknown_amounts(text, TURN)
    assert unknown == ["$20.00"]
    assert strip_amounts(text, unknown) == "Your subtotal is 129.00 USD and you save the amount shown below."
