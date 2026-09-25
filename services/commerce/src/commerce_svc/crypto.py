"""Column-level encryption for shipping addresses (PII at rest, docs §10)."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet


class AddressCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, address: dict[str, Any]) -> bytes:
        return self._fernet.encrypt(json.dumps(address, sort_keys=True).encode())

    def decrypt(self, token: bytes) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self._fernet.decrypt(token))
        return data
