"""API fuzzing with Schemathesis (docs §11): property-based requests generated from each service's
published OpenAPI, sent to the REAL app (in-process, real Postgres) through an async httpx client, so the
app runs on pytest's event loop like it does in production.

Checks on every response: no 5xx (``not_a_server_error``) and documented responses match their schema
(``response_schema_conformance``). A failure prints a curl command that reproduces it.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import schemathesis
from contract_kit import OPENAPI_DIR, load
from hypothesis import HealthCheck, settings
from schemathesis.checks import not_a_server_error
from schemathesis.specs.openapi.checks import response_schema_conformance

FUZZ_SETTINGS = settings(
    max_examples=int(os.environ.get("FUZZ_EXAMPLES", "15")),  # CI: 15/operation; deep runs: 200+
    deadline=None,
    database=None,  # reproducible from the seed printed on failure; no local example DB
    suppress_health_check=[
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
    ],
)
CHECKS = [not_a_server_error, response_schema_conformance]


def operations(service: str) -> list[Any]:
    schema = schemathesis.openapi.from_dict(load(OPENAPI_DIR / f"{service}.json"))
    return [result.ok() for result in schema.get_all_operations()]


def operation_id(operation: Any) -> str:
    return f"{operation.method.upper()} {operation.path}"


async def call_and_validate(client: httpx.AsyncClient, case: Any, headers: dict[str, str]) -> httpx.Response:
    kwargs = case.as_transport_kwargs(base_url=str(client.base_url), headers=headers)
    kwargs.pop("cookies", None)
    response = await client.request(**kwargs)
    try:
        case.validate_response(response, checks=CHECKS)
    except Exception as exc:  # enrich with a copy-pasteable reproduction
        raise AssertionError(f"{exc}\n\nReproduce: {case.as_curl_command(headers=headers)}") from exc
    return response
