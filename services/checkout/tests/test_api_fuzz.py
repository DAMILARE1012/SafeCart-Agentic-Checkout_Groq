"""Schemathesis fuzzing of checkout-svc's published API (contracts/openapi/checkout.json).

The Stripe webhook endpoint is fuzzed too: every generated request lacks a valid signature, so each one
must be rejected cleanly (4xx), never crash the service.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from checkout_testkit import AGENT, GATEWAY
from fuzz_kit import FUZZ_SETTINGS, call_and_validate, operation_id, operations
from hypothesis import given
from hypothesis import strategies as st

OPERATIONS = operations("checkout")


@pytest.mark.parametrize("operation", OPERATIONS, ids=[operation_id(o) for o in OPERATIONS])
@FUZZ_SETTINGS
@given(data=st.data())
async def test_checkout_api_never_errors_and_matches_its_schema(
    operation: Any, data: st.DataObject, checkout: httpx.AsyncClient
) -> None:
    case = data.draw(operation.as_strategy())
    headers = AGENT if operation.method.upper() == "GET" and "/conversations/" in operation.path else GATEWAY
    response = await call_and_validate(checkout, case, headers)
    if operation.path == "/webhooks/stripe":
        assert 400 <= response.status_code < 500  # unsigned: always refused
