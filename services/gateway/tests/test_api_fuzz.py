"""Schemathesis fuzzing of gateway-svc's public API (contracts/openapi/gateway.json): the only service a
browser can reach, so it must turn any input into a clean 4xx, never a 5xx."""

from __future__ import annotations

from typing import Any

import pytest
from fuzz_kit import FUZZ_SETTINGS, call_and_validate, operation_id, operations
from gateway_testkit import ORIGIN, new_session
from hypothesis import given
from hypothesis import strategies as st

# Telegram is suspended (not mounted); the rest is the widget-facing API.
OPERATIONS = [o for o in operations("gateway") if not o.path.startswith("/telegram")]


@pytest.mark.parametrize("operation", OPERATIONS, ids=[operation_id(o) for o in OPERATIONS])
@FUZZ_SETTINGS
@given(data=st.data())
async def test_gateway_api_never_errors_and_matches_its_schema(
    operation: Any, data: st.DataObject, gateway: Any
) -> None:
    case = data.draw(operation.as_strategy())
    async with gateway() as gw:
        session = await new_session(gw)
        headers = {"Origin": ORIGIN, "Authorization": f"Bearer {session['session_token']}"}
        await call_and_validate(gw, case, headers)
