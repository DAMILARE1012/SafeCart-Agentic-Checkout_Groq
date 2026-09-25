"""Create `.env` from `.env.example`, filling every generated secret with a fresh random value.

    python scripts/init_env.py            # creates .env (refuses to overwrite an existing one)
    python scripts/init_env.py --force    # regenerates .env from the template

Generated: database/Redis/RabbitMQ/Grafana passwords, the service-to-service API keys and their
SHA-256 hashes, the JWT secret, the Telegram webhook secret and the Fernet field-encryption key.
Still yours to paste in afterwards: GROQ_API_KEY and the Stripe test-mode keys (printed at the end).
Standard library only, so it runs before `uv sync`.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE, TARGET = ROOT / ".env.example", ROOT / ".env"

# Hex only: these end up inside connection URLs, so no characters that need escaping.
PASSWORDS = (
    "POSTGRES_SUPERUSER_PASSWORD",
    "COMMERCE_DB_PASSWORD",
    "CHECKOUT_DB_PASSWORD",
    "FULFILLMENT_DB_PASSWORD",
    "AGENT_DB_PASSWORD",
    "REDIS_PASSWORD",
    "RABBITMQ_PASSWORD",
    "GRAFANA_ADMIN_PASSWORD",
)
SERVICE_KEYS = ("GATEWAY_SVC_API_KEY", "AGENT_SVC_API_KEY", "CHECKOUT_SVC_API_KEY")
USER_SUPPLIED = {
    "GROQ_API_KEY": "https://console.groq.com/keys",
    "STRIPE_SECRET_KEY": "https://dashboard.stripe.com/test/apikeys (sk_test_...)",
    "STRIPE_PUBLISHABLE_KEY": "https://dashboard.stripe.com/test/apikeys (pk_test_...)",
    "STRIPE_WEBHOOK_SECRET": "docker compose --profile dev-tools run --rm stripe-cli listen --print-secret",
}


def generated_values() -> dict[str, str]:
    values = {name: secrets.token_hex(16) for name in PASSWORDS}
    for name in SERVICE_KEYS:
        key = secrets.token_urlsafe(32)
        values[name] = key
        values[f"{name}_HASH"] = hashlib.sha256(key.encode()).hexdigest()
    values["JWT_SECRET"] = secrets.token_urlsafe(48)
    values["TELEGRAM_WEBHOOK_SECRET"] = secrets.token_urlsafe(32)
    values["FIELD_ENCRYPTION_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode()  # Fernet format
    return values


def render(template: str, values: dict[str, str]) -> str:
    """Replaces only the value of each `NAME=value  # comment` line, keeping comments and layout."""
    line = re.compile(r"^(?P<name>[A-Z0-9_]+)=(?P<value>\S*)(?P<rest>.*)$")

    def fill(match: re.Match[str]) -> str:
        name = match["name"]
        if name not in values:
            return match[0]
        return f"{name}={values[name]}{match['rest']}"

    return "\n".join(line.sub(fill, row) for row in template.splitlines()) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = parser.parse_args()

    if TARGET.exists() and not args.force:
        print(f"{TARGET.name} already exists; nothing changed (--force regenerates it).", file=sys.stderr)
        return 1
    values = generated_values()
    TARGET.write_text(render(TEMPLATE.read_text(encoding="utf-8"), values), encoding="utf-8")

    print(f"Created {TARGET.name} with {len(values)} generated secrets.")
    print("Now add these to .env yourself:")
    for name, source in USER_SUPPLIED.items():
        print(f"  {name:<24} {source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
