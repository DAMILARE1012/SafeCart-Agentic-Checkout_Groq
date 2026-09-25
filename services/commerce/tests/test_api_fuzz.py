"""Schemathesis fuzzing of commerce-svc's published API (contracts/openapi/commerce.json)."""

from __future__ import annotations

from typing import Any

import pytest
from fuzz_kit import FUZZ_SETTINGS, call_and_validate, operation_id, operations
from httpx import AsyncClient
from hypothesis import given
from hypothesis import strategies as st

OPERATIONS = operations("commerce")


@pytest.mark.parametrize("operation", OPERATIONS, ids=[operation_id(o) for o in OPERATIONS])
@FUZZ_SETTINGS
@given(data=st.data())
async def test_commerce_api_never_errors_and_matches_its_schema(
    operation: Any, data: st.DataObject, client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    case = data.draw(operation.as_strategy())
    # The settlement API belongs to checkout-svc's key; everything else to agent-svc's.
    headers = checkout if operation.path.startswith("/internal/") else agent
    await call_and_validate(client, case, headers)


@pytest.mark.parametrize("q", ["\x00", "shoe\x00s", "trail\x1f"])
async def test_control_characters_in_search_are_rejected_cleanly(
    q: str, client: AsyncClient, agent: dict[str, str]
) -> None:
    """Found by fuzzing (intermittently: only some random inputs contain NUL): used to be a 500."""
    response = await client.get("/v1/products/search", params={"q": q}, headers=agent)
    assert response.status_code == 422
