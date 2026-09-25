"""Operational alerts (docs §11): things a human must look at, e.g. an order stuck in manual review.

Every alert is logged at ERROR (or WARNING). When ``ALERT_WEBHOOK_URL`` is set it is also POSTed as a
Slack-compatible ``{"text": ...}`` message (Slack, Mattermost, Rocket.Chat, and most incident tools
accept that shape), with the structured fields alongside under ``"alert"``.

``send`` returns True only once the alert has been delivered, so callers that need durability (mark
an order as "alerted") update their marker only on success and retry otherwise. Repeats of the same
``key`` inside the de-duplication window are suppressed so a periodic check can't flood the channel.
"""

from __future__ import annotations

import time
from typing import Any, Literal

import httpx
import structlog

log = structlog.get_logger("alerts")

Severity = Literal["critical", "warning"]


class Alerter:
    def __init__(
        self,
        webhook_url: str | None,
        *,
        source: str,
        http: httpx.AsyncClient | None = None,
        dedup_seconds: float = 3600,
    ) -> None:
        self._url = webhook_url or None
        self._source = source
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._dedup_seconds = dedup_seconds
        self._sent: dict[str, float] = {}

    async def send(
        self,
        key: str,
        title: str,
        *,
        severity: Severity = "critical",
        details: dict[str, Any] | None = None,
        dedup: bool = True,
    ) -> bool:
        now = time.monotonic()
        if dedup and now - self._sent.get(key, -self._dedup_seconds) < self._dedup_seconds:
            return True  # already told someone recently
        details = details or {}
        emit = log.error if severity == "critical" else log.warning
        emit("alert", alert_key=key, title=title, severity=severity, **details)
        if self._url:
            body = "\n".join(f"• {k}: {v}" for k, v in details.items())
            payload = {
                "text": f"[{severity.upper()}] {self._source}: {title}" + (f"\n{body}" if body else ""),
                "alert": {
                    "key": key,
                    "title": title,
                    "severity": severity,
                    "source": self._source,
                    **details,
                },
            }
            try:
                response = await self._http.post(self._url, json=payload)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("alert_delivery_failed", alert_key=key, error=type(exc).__name__)
                return False
        self._sent[key] = now
        return True

    async def aclose(self) -> None:
        await self._http.aclose()
