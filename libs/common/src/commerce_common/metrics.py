"""Business and reliability metrics (docs §11), exported through OpenTelemetry → collector → Prometheus.

Instruments come from the global meter, which is a no-op proxy until ``setup_telemetry`` /
``setup_worker_telemetry`` installs a provider (OTEL_ENABLED + METRICS_ENABLED). So code can record
metrics unconditionally: in tests and with telemetry off, it costs nothing.

Naming: dotted OTel names; the collector's Prometheus exporter turns ``agent.llm.duration`` (unit ``s``)
into ``agent_llm_duration_seconds`` and counters into ``…_total``. Keep label values low-cardinality
(never ids, amounts or free text).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import cache

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Counter, Histogram, Observation, _Gauge

_meter = metrics.get_meter("commerce-agent")

# Counters to create at zero once telemetry starts (see ``prime``): (counter, label sets).
_PRIMED: list[tuple[Counter, list[dict[str, str]]]] = []

# Seconds buckets that suit both fast internal calls and multi-second LLM turns.
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0, 30.0, 60.0)


@cache
def counter(name: str, description: str, unit: str = "1") -> Counter:
    return _meter.create_counter(name, unit=unit, description=description)


@cache
def histogram(name: str, description: str, unit: str = "s") -> Histogram:
    return _meter.create_histogram(
        name, unit=unit, description=description, explicit_bucket_boundaries_advisory=list(LATENCY_BUCKETS)
    )


@cache
def gauge(name: str, description: str, unit: str = "{count}") -> _Gauge:
    # A {annotation} unit adds no Prometheus suffix ("1" would append a misleading "_ratio").
    return _meter.create_gauge(name, unit=unit, description=description)


def observed(name: str, description: str, read: Callable[[], float | None], unit: str = "{count}") -> None:
    """A gauge whose current value is READ at every export, for values computed rarely (e.g. by a
    15-minute reconciliation). A set-once gauge would go stale and be dropped by the collector."""

    def callback(_: CallbackOptions) -> Iterable[Observation]:
        value = read()
        return [] if value is None else [Observation(value)]

    _meter.create_observable_gauge(name, callbacks=[callback], unit=unit, description=description)


def prime_labels(instrument: Counter, label_sets: Iterable[dict[str, str]] = ({},)) -> Counter:
    """Declare the label values a counter will use, so its series exist at 0 from process start.

    Without this a series appears on its FIRST increment, already at 1 (or at an order's amount), and
    ``increase()`` in Prometheus cannot see that first step: after every deploy the first paid order or
    refund would silently vanish from the dashboards (seen live before this was added).
    """
    _PRIMED.append((instrument, list(label_sets)))
    return instrument


def prime() -> None:
    """Called by setup_telemetry / setup_worker_telemetry right after the meter provider is installed."""
    for instrument, label_sets in _PRIMED:
        for labels in label_sets:
            instrument.add(0, labels)
