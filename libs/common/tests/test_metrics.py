"""Primed counters exist at 0 from startup, so Prometheus' increase() sees the FIRST real event."""

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from commerce_common.metrics import counter, prime, prime_labels


def points(reader: InMemoryMetricReader, name: str) -> dict[str, float]:
    data = reader.get_metrics_data()
    out: dict[str, float] = {}
    for resource in data.resource_metrics if data else []:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        out[str(dict(point.attributes or {}))] = point.value
    return out


def test_primed_counter_starts_at_zero_then_counts_the_first_event() -> None:
    refunds = prime_labels(counter("test.refunds", "test"), [{"outcome": "created"}, {"outcome": "rejected"}])
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))  # what setup_telemetry does
    prime()

    assert points(reader, "test.refunds") == {"{'outcome': 'created'}": 0, "{'outcome': 'rejected'}": 0}
    refunds.add(1, {"outcome": "created"})
    assert points(reader, "test.refunds")["{'outcome': 'created'}"] == 1  # 0 → 1: a visible increase
