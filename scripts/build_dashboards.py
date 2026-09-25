"""Generate the provisioned Grafana dashboards (infra/grafana/dashboards/*.json) from one readable spec.

    python scripts/build_dashboards.py

Dashboards are code: edit the panels here, regenerate, review the JSON diff. Metric names are the ones
the OTel collector exports to Prometheus (see commerce_common/metrics.py and each service's instruments).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "infra" / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}


class Board:
    def __init__(self, uid: str, title: str, description: str) -> None:
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict[str, Any]] = []
        self.x = self.y = self.row_height = 0

    def _place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.x, self.y = 0, self.y + self.row_height
            self.row_height = 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_height = max(self.row_height, h)
        return pos

    def row(self, title: str) -> None:
        if self.x:
            self.x, self.y = 0, self.y + self.row_height
        self.panels.append(
            {"type": "row", "title": title, "collapsed": False, "id": len(self.panels) + 1,
             "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []}
        )  # fmt: skip
        self.y += 1
        self.row_height = 0

    def stat(self, title: str, expr: str, *, unit: str = "short", w: int = 4, description: str = "",
             thresholds: list[tuple[float | None, str]] | None = None, mappings: list[dict[str, Any]] | None = None,
             decimals: int | None = None) -> None:  # fmt: skip
        steps = [{"color": color, "value": value} for value, color in (thresholds or [(None, "green")])]
        self.panels.append(
            {
                "type": "stat", "title": title, "description": description, "id": len(self.panels) + 1,
                "datasource": DS, "gridPos": self._place(w, 4),
                "targets": [{"refId": "A", "expr": expr, "datasource": DS, "instant": True}],
                "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}, "colorMode": "background",
                            "graphMode": "none", "textMode": "value"},
                "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "mappings": mappings or [],
                                             "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
            }
        )  # fmt: skip

    def series(self, title: str, targets: list[tuple[str, str]], *, unit: str = "short", w: int = 12,
               description: str = "", stacked: bool = False, limit: float | None = None) -> None:  # fmt: skip
        steps: list[dict[str, Any]] = [{"color": "green", "value": None}]
        custom: dict[str, Any] = {"lineWidth": 2, "fillOpacity": 18 if stacked else 8, "showPoints": "never",
                                  "stacking": {"mode": "normal" if stacked else "none"}}  # fmt: skip
        if limit is not None:  # draw the limit as a red dashed threshold line
            steps.append({"color": "red", "value": limit})
            custom["thresholdsStyle"] = {"mode": "dashed"}
        self.panels.append(
            {
                "type": "timeseries", "title": title, "description": description, "id": len(self.panels) + 1,
                "datasource": DS, "gridPos": self._place(w, 8),
                "targets": [{"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "datasource": DS}
                            for i, (expr, legend) in enumerate(targets)],
                "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                            "tooltip": {"mode": "multi", "sort": "desc"}},
                "fieldConfig": {"defaults": {"unit": unit, "custom": custom,
                                             "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []},
            }
        )  # fmt: skip

    def write(self) -> Path:
        doc = {
            "uid": self.uid, "title": self.title, "description": self.description,
            "tags": ["commerce-agent"], "timezone": "browser", "schemaVersion": 41, "version": 1,
            "refresh": "30s", "time": {"from": "now-6h", "to": "now"}, "editable": False,
            "panels": self.panels,
        }  # fmt: skip
        path = OUT / f"{self.uid}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        return path


def business() -> Board:
    b = Board(
        "commerce-business", "Commerce · Business & money", "Revenue, refunds and the conversion funnel."
    )
    b.row("Money (selected time range)")
    b.stat("Paid volume", 'sum(increase(checkout_paid_amount_total{currency="USD"}[$__range])) / 100 or vector(0)',
           unit="currencyUSD", decimals=2)  # fmt: skip
    b.stat("Refunded volume", 'sum(increase(checkout_refunded_amount_total{currency="USD"}[$__range])) / 100 '
           "or vector(0)", unit="currencyUSD", decimals=2, thresholds=[(None, "green"), (0.01, "orange")])  # fmt: skip
    b.stat("Orders paid", 'sum(increase(checkout_order_transitions_total{to="PAID"}[$__range])) or vector(0)',
           decimals=0)  # fmt: skip
    b.stat(
        "Refund rate",
        '(sum(increase(checkout_order_transitions_total{to="REFUNDED"}[$__range])) or vector(0)) '
        '/ clamp_min(sum(increase(checkout_order_transitions_total{to="PAID"}[$__range])), 1)',
        unit="percentunit",
        thresholds=[(None, "green"), (0.05, "orange"), (0.15, "red")],
        description="Share of paid orders that were refunded because they could not be fulfilled.",
    )
    b.stat("Stripe mismatches", "sum(increase(checkout_reconciliation_mismatches_total[$__range])) or vector(0)",
           decimals=0, thresholds=[(None, "green"), (1, "red")],
           description="Orders whose amount disagreed with what Stripe received (reconciliation).")  # fmt: skip
    b.stat("Audit chain", "min(checkout_audit_chain_ok)", thresholds=[(None, "red"), (1, "green")],
           mappings=[{"type": "value", "options": {"0": {"text": "BROKEN"}, "1": {"text": "INTACT"}}}],
           description="Hash chain re-verified by every reconciliation run.")  # fmt: skip
    b.row("Funnel and outcomes")
    b.series(
        "Conversion funnel (per 5 min)",
        [
            ('sum(increase(agent_turns_total{outcome="ok"}[5m]))', "chat turns"),
            ("sum(increase(commerce_quotes_created_total[5m]))", "order summaries"),
            ('sum(increase(checkout_order_transitions_total{to="AWAITING_PAYMENT"}[5m]))', "Confirm & pay"),
            ('sum(increase(checkout_order_transitions_total{to="PAID"}[5m]))', "paid"),
            ('sum(increase(checkout_order_transitions_total{to="FULFILLED"}[5m]))', "fulfilled"),
        ],
        description="Chat → quote → explicit confirmation → payment → fulfilment.",
    )
    b.series(
        "Order outcomes (per 5 min)",
        [('sum by (to) (increase(checkout_order_transitions_total{to=~"FULFILLED|REFUNDED|EXPIRED|PAYMENT_FAILED'
          '|CANCELED|MANUAL_REVIEW"}[5m]))', "{{to}}")],
        stacked=True,
    )  # fmt: skip
    b.series("Compensation refunds (per 5 min)", [("sum by (outcome) (increase(checkout_refunds_total[5m]))",
             "{{outcome}}")], stacked=True)  # fmt: skip
    b.series("Alerts raised (per hour)", [("sum by (kind) (increase(alerts_sent_total[1h]))", "{{kind}}")],
             stacked=True, description="Orders needing review, missed webhooks, dead letters, tampering…")  # fmt: skip
    return b


def reliability() -> Board:
    b = Board(
        "commerce-reliability", "Commerce · Reliability & agent", "Pipelines, services and the LLM agent."
    )
    b.row("Payments pipeline")
    b.stat("Outbox backlog", "max(checkout_outbox_backlog) or vector(0)", decimals=0, w=3,
           thresholds=[(None, "green"), (5, "orange"), (20, "red")])  # fmt: skip
    b.stat("Webhook backlog", "max(checkout_webhooks_backlog) or vector(0)", decimals=0, w=3,
           thresholds=[(None, "green"), (5, "orange"), (20, "red")])  # fmt: skip
    b.stat("Dead webhooks", "max(checkout_webhooks_dead) or vector(0)", decimals=0, w=3,
           thresholds=[(None, "green"), (1, "red")],
           description="Failed every retry. After fixing the cause: python -m checkout_svc.ops retry-dead-webhooks")  # fmt: skip
    b.stat("Dead letters", 'sum(rabbitmq_queue_messages{queue=~".*dlx.*"}) or vector(0)', decimals=0, w=3,
           thresholds=[(None, "green"), (1, "red")])  # fmt: skip
    b.stat("Stuck orders", "max(checkout_orders_stuck) or vector(0)", decimals=0, w=3,
           thresholds=[(None, "green"), (1, "orange")])  # fmt: skip
    b.stat("Webhook lag p95", "histogram_quantile(0.95, sum by (le) (rate(checkout_webhook_lag_seconds_bucket[15m])))",
           unit="s", w=3, thresholds=[(None, "green"), (5, "orange"), (30, "red")])  # fmt: skip
    b.stat("Signature failures", "sum(increase(checkout_webhooks_rejected_total[$__range])) or vector(0)",
           decimals=0, w=3, thresholds=[(None, "green"), (1, "orange")])  # fmt: skip
    b.series("Stripe webhooks (per 5 min)", [
        ('sum by (type) (increase(checkout_webhooks_received_total{duplicate="false"}[5m]))', "{{type}}"),
        ('sum(increase(checkout_webhooks_received_total{duplicate="true"}[5m]))', "duplicates (ignored)"),
    ])  # fmt: skip
    b.series("Queue depth", [("sum by (queue) (rabbitmq_queue_messages)", "{{queue}}")],
             description="Durable quorum queues; the .dlx.q queue holds dead letters.")  # fmt: skip
    b.series("Fulfilment outcomes (per 5 min)", [("sum by (status) (increase(fulfillment_jobs_total[5m]))",
             "{{status}}")], stacked=True)  # fmt: skip
    b.series("Warehouse latency", [
        ("histogram_quantile(0.5, sum by (le) (rate(fulfillment_provider_duration_seconds_bucket[5m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(fulfillment_provider_duration_seconds_bucket[5m])))", "p95"),
    ], unit="s")  # fmt: skip
    b.row("Services")
    b.series("Requests per second by service", [
        ("sum by (job) (rate(http_server_duration_milliseconds_count[5m]))", "{{job}}")], unit="reqps")  # fmt: skip
    b.series("HTTP 5xx per second by service", [
        ('sum by (job, http_status_code) (rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[5m]))',
         "{{job}} {{http_status_code}}")], unit="reqps", limit=0.1,
        description="500 = a bug; 503 = a dependency was unavailable and the caller may retry.")  # fmt: skip
    b.series("p95 latency by service", [
        ('histogram_quantile(0.95, sum by (job, le) (rate(http_server_duration_milliseconds_bucket'
         '{http_target!~".*/(turns|events)"}[5m])))', "{{job}}")], unit="ms", w=24,
        description="Request/response endpoints. Streaming chat turns are excluded: see LLM latency.")  # fmt: skip
    b.row("Agent (LLM on Groq)")
    b.series("Turns by outcome (per 5 min)", [("sum by (outcome) (increase(agent_turns_total[5m]))", "{{outcome}}")],
             stacked=True)  # fmt: skip
    b.series("LLM latency", [
        ("histogram_quantile(0.5, sum by (model, le) (rate(agent_llm_duration_seconds_bucket[5m])))", "p50 {{model}}"),
        ("histogram_quantile(0.95, sum by (model, le) (rate(agent_llm_duration_seconds_bucket[5m])))", "p95 {{model}}"),
    ], unit="s")  # fmt: skip
    b.series("Tokens per minute by model", [("sum by (model) (rate(agent_llm_tokens_total[5m])) * 60", "{{model}}")],
             limit=8000, description="Red line: Groq free-tier limit of 8k tokens/min per model.")  # fmt: skip
    b.series("Guardrail interventions (per 15 min)", [
        ("sum by (guard, action) (increase(agent_guardrail_triggers_total[15m]))", "{{guard}} · {{action}}")],
        stacked=True)  # fmt: skip
    b.series("Tool calls (per 15 min)", [
        ("sum by (tool, ok) (increase(agent_tool_calls_total[15m]))", "{{tool}} ok={{ok}}")], stacked=True)  # fmt: skip
    b.series("LLM errors and rate limiting (per 15 min)", [
        ("sum by (kind) (increase(agent_llm_errors_total[15m])) or vector(0)", "llm {{kind}}"),
        ("sum(increase(gateway_rate_limited_total[15m])) or vector(0)", "gateway rate-limited"),
    ])  # fmt: skip
    return b


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for board in (business(), reliability()):
        print(f"{board.write().relative_to(OUT.parent.parent.parent)}  ({len(board.panels)} panels)")
