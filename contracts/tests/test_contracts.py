"""Contract snapshots: the code must match what is published in contracts/ (docs §11).

If one of these fails, you changed an API or event. Breaking event changes need a new version
(``order.paid.v2`` published side by side). Otherwise regenerate the snapshots
(``uv run python scripts/export_contracts.py``) and review the diff with the consumers in mind.
"""

from __future__ import annotations

import pytest
from contract_kit import (
    EVENTS_DIR,
    OPENAPI_DIR,
    breaking_changes,
    dump,
    event_schemas,
    load,
    openapi_documents,
)

REGENERATE = "run `uv run python scripts/export_contracts.py` and review the diff"


@pytest.fixture(scope="module")
def documents() -> dict[str, dict]:
    return openapi_documents()


@pytest.mark.parametrize("service", ["commerce", "checkout", "gateway", "agent"])
def test_openapi_matches_published_snapshot(service: str, documents: dict[str, dict]) -> None:
    published = (OPENAPI_DIR / f"{service}.json").read_text(encoding="utf-8")
    assert dump(documents[service]) == published, f"{service} API changed: {REGENERATE}"


@pytest.mark.parametrize("event", sorted(event_schemas()))
def test_versioned_events_only_change_additively(event: str) -> None:
    published = load(EVENTS_DIR / f"{event}.json")
    current = event_schemas()[event]
    assert not breaking_changes(published, current), (
        f"{event}: breaking change {breaking_changes(published, current)}; publish a new version instead"
    )
    assert dump(current) == dump(published), f"{event} changed compatibly: {REGENERATE}"


def test_compatibility_checker_catches_breaking_changes() -> None:
    published = {
        "properties": {"order_id": {"type": "string"}, "total": {"type": "integer"}},
        "required": ["order_id"],
    }
    assert breaking_changes(published, {**published, "properties": {"order_id": {"type": "string"}}}) == [
        "total: removed"
    ]
    changed = {
        "properties": {"order_id": {"type": "integer"}, "total": {"type": "integer"}},
        "required": ["order_id"],
    }
    assert breaking_changes(published, changed) == ["order_id: type changed"]
    stricter = {**published, "required": ["order_id", "total"]}
    assert breaking_changes(published, stricter) == ["total: newly required"]
    additive = {**published, "properties": {**published["properties"], "note": {"type": "string"}}}
    assert breaking_changes(published, additive) == []
