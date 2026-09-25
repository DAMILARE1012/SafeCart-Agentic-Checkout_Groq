"""Builds every contract document from the CODE, so committed snapshots can be diffed against it.

- ``openapi/<service>.json``: each service's HTTP API as FastAPI publishes it (built offline: apps are
  constructed with inert settings, nothing connects).
- ``events/<type>.json``: JSON Schema of every versioned event payload plus the envelope.
- ``widget/gateway-api.schema.json``: the widget's own expectations of the gateway (generated from
  ``widget/src/shared/api/contracts.ts`` by ``npm run contracts``), verified against real provider
  responses in the service test suites (consumer-driven contract).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from commerce_common import events

ROOT = Path(__file__).resolve().parent
OPENAPI_DIR = ROOT / "openapi"
EVENTS_DIR = ROOT / "events"
WIDGET_SCHEMA = ROOT / "widget" / "gateway-api.schema.json"

_HASH = "0" * 64
_URL = "postgresql+asyncpg://contracts:contracts@localhost:1/contracts"  # never connected to

EVENT_MODELS: dict[str, type[BaseModel]] = {
    "envelope": events.Envelope,
    events.ORDER_PAID: events.OrderPaidV1,
    events.FULFILLMENT_SUCCEEDED: events.FulfillmentSucceededV1,
    events.FULFILLMENT_FAILED: events.FulfillmentFailedV1,
}


def _apps() -> dict[str, Any]:
    from cryptography.fernet import Fernet

    from agent_svc.main import create_app as agent_app
    from agent_svc.settings import AgentSettings
    from checkout_svc.main import create_app as checkout_app
    from checkout_svc.settings import CheckoutSettings
    from commerce_svc.main import create_app as commerce_app
    from commerce_svc.settings import CommerceSettings
    from gateway_svc.main import create_app as gateway_app
    from gateway_svc.settings import GatewaySettings

    quiet: dict[str, Any] = {"app_env": "test", "log_level": "WARNING", "log_format": "console"}
    return {
        "commerce": commerce_app(
            CommerceSettings(
                **quiet,
                database_url=_URL,
                field_encryption_key=Fernet.generate_key().decode(),
                agent_svc_api_key_hash=_HASH,
                checkout_svc_api_key_hash=_HASH,
            )
        ),
        "checkout": checkout_app(
            CheckoutSettings(
                **quiet,
                database_url=_URL,
                commerce_service_url="http://commerce",
                service_api_key="contracts",
                gateway_svc_api_key_hash=_HASH,
                agent_svc_api_key_hash=_HASH,
                stripe_secret_key="sk_test_" + "c" * 24,
                stripe_webhook_secret="whsec_" + "c" * 24,
            )
        ),
        "gateway": gateway_app(
            GatewaySettings(
                **quiet,
                agent_service_url="http://agent",
                checkout_service_url="http://checkout",
                service_api_key="contracts",
                jwt_secret="c" * 40,
                widget_publishable_key="pk_contracts",
            )
        ),
        "agent": agent_app(
            AgentSettings(
                **quiet,
                langgraph_checkpointer="memory",
                commerce_service_url="http://commerce",
                service_api_key="contracts",
                gateway_svc_api_key_hash=_HASH,
                groq_api_key="contracts",
            )
        ),
    }


def openapi_documents() -> dict[str, dict[str, Any]]:
    return {name: app.openapi() for name, app in _apps().items()}


def event_schemas() -> dict[str, dict[str, Any]]:
    return {name: model.model_json_schema() for name, model in EVENT_MODELS.items()}


def dump(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# ---------------------------------------------------------------------------
# Versioned event compatibility: a published version may only change ADDITIVELY
# ---------------------------------------------------------------------------
def _resolve(schema: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref and ref.startswith("#/$defs/"):
        return dict(schema.get("$defs", {})[ref.split("/")[-1]])
    return node


def _shape(schema: dict[str, Any], node: dict[str, Any]) -> Any:
    node = _resolve(schema, node)
    if "anyOf" in node:
        return sorted(str(_shape(schema, n)) for n in node["anyOf"])
    if node.get("type") == "array":
        return ["array", _shape(schema, node.get("items", {}))]
    if node.get("type") == "object" or "properties" in node:
        return {k: _shape(schema, v) for k, v in node.get("properties", {}).items()}
    return node.get("type", node.get("format", "any"))


def breaking_changes(published: dict[str, Any], current: dict[str, Any], path: str = "") -> list[str]:
    """What would break a consumer still reading ``published``: removed fields, changed types, or
    newly required fields (old producers don't send them). Adding optional fields is fine."""
    problems: list[str] = []
    old_props, new_props = published.get("properties", {}), current.get("properties", {})
    for name, old in old_props.items():
        where = f"{path}{name}"
        if name not in new_props:
            problems.append(f"{where}: removed")
            continue
        old_node, new_node = _resolve(published, old), _resolve(current, new_props[name])
        if "properties" in old_node or old_node.get("type") == "object":
            problems += breaking_changes(
                {**old_node, "$defs": published.get("$defs", {})},
                {**new_node, "$defs": current.get("$defs", {})},
                f"{where}.",
            )
        elif _shape(published, old) != _shape(current, new_props[name]):
            problems.append(f"{where}: type changed")
    for name in set(current.get("required", [])) - set(published.get("required", [])):
        problems.append(f"{path}{name}: newly required")
    return problems


# ---------------------------------------------------------------------------
# Consumer-driven: validate provider output against the widget's own schema
# ---------------------------------------------------------------------------
def widget_validator(definition: str) -> Any:
    from jsonschema import Draft7Validator

    document = load(WIDGET_SCHEMA)
    return Draft7Validator({"$ref": f"#/definitions/{definition}", "definitions": document["definitions"]})


def assert_matches_widget(definition: str, payload: Any) -> None:
    from jsonschema.exceptions import best_match

    validator = widget_validator(definition)
    errors = list(validator.iter_errors(payload))
    if not errors:
        return
    # For unions (anyOf), best_match descends into the branch that came closest, i.e. the real cause.
    error = best_match(errors)
    raise AssertionError(
        f"{definition} violates the widget contract at "
        f"{'/'.join(map(str, error.absolute_path)) or '<root>'}: {error.message[:300]}"
    )
