"""Regenerate the committed contract snapshots in contracts/ from the code.

    uv run python scripts/export_contracts.py          # OpenAPI + event schemas
    (cd widget && npm run contracts)                    # widget → gateway JSON Schema

Run it when you change an API or event on purpose, then review the diff like any other code change.
The contract tests fail whenever the code and these snapshots disagree.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))

from contract_kit import EVENTS_DIR, OPENAPI_DIR, dump, event_schemas, openapi_documents


def main() -> None:
    OPENAPI_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    for name, document in openapi_documents().items():
        (OPENAPI_DIR / f"{name}.json").write_text(dump(document), encoding="utf-8")
        print(f"openapi/{name}.json  ({len(document.get('paths', {}))} paths)")
    for name, schema in event_schemas().items():
        (EVENTS_DIR / f"{name}.json").write_text(dump(schema), encoding="utf-8")
        print(f"events/{name}.json")


if __name__ == "__main__":
    main()
