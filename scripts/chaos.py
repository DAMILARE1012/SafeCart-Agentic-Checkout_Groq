"""Live chaos tests against the running stack (docs §11): break real infrastructure mid-flow, then prove
nothing was lost or duplicated.

    python scripts/chaos.py                  # all scenarios
    python scripts/chaos.py broker worker    # some of them

Needs: the stack running (docker compose --profile app --profile dev-tools up -d) with Stripe TEST keys.
Each scenario places a real order, pays it with a real Stripe test-mode PaymentIntent, and injects a
fault with docker (pause / kill / stop). Afterwards the invariants are checked across all its orders.
Standard library only.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DRIVER = (ROOT / "scripts" / "chaos" / "driver.py").read_text(encoding="utf-8")
SKU = "sku_merino_socks_m"  # not in FULFILLMENT_MOCK_FAIL_SKUS: these orders must be fulfilled


def env_value(name: str) -> str:
    match = re.search(rf"^{name}=([^\s#]+)", (ROOT / ".env").read_text(encoding="utf-8"), re.M)
    if not match:
        sys.exit(f"{name} missing from .env")
    return match.group(1)


def compose(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed: {result.stderr[-500:]}")
    return result.stdout


def driver(command: dict[str, Any]) -> dict[str, Any]:
    """Runs one driver command inside checkout-api (keys go in as env vars, never printed)."""
    result = subprocess.run(
        [
            "docker", "compose", "exec", "-T",
            "-e", f"CHAOS_AGENT_KEY={env_value('AGENT_SVC_API_KEY')}",
            "-e", f"CHAOS_GATEWAY_KEY={env_value('GATEWAY_SVC_API_KEY')}",
            "-e", f"CHAOS_CMD={json.dumps(command)}",
            "checkout-api", "python", "-",
        ],
        cwd=ROOT, input=DRIVER, capture_output=True, text=True, encoding="utf-8",
    )  # fmt: skip
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith("CHAOS_RESULT ")), None)
    if line is None:
        raise RuntimeError(f"driver {command['cmd']} failed:\n{result.stderr[-1500:]}")
    data: dict[str, Any] = json.loads(line.removeprefix("CHAOS_RESULT "))
    return data


def wait_for(order_id: str, want: str, timeout_s: float) -> tuple[str, float]:
    started = time.monotonic()
    status = "?"
    while time.monotonic() - started < timeout_s:
        status = driver({"cmd": "status", "order_id": order_id})["status"]
        if status == want:
            break
        time.sleep(2)
    return status, time.monotonic() - started


def queue_depth(name: str) -> int:
    vhost = env_value("RABBITMQ_VHOST")
    out = compose(
        "exec", "-T", "rabbitmq", "rabbitmqctl", "list_queues", "-q", "-p", vhost, "name", "messages"
    )
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == name:
            return int(parts[1])
    return 0


# ------------------------------------------------------------------------------------------ scenarios
def scenario_broker(log: Callable[[str], None]) -> str:
    """RabbitMQ unreachable while an order is paid: the event waits in the outbox, then flows."""
    order = driver({"cmd": "prepare", "sku": SKU})["order_id"]
    compose("pause", "rabbitmq")
    try:
        driver({"cmd": "pay", "order_id": order})
        status, _ = wait_for(order, "PAID", 30)
        log(f"broker paused → order {status} (fulfilment can't be requested yet)")
        time.sleep(10)
        still = driver({"cmd": "status", "order_id": order})["status"]
        log(f"after 10 s with the broker down → still {still}")
    finally:
        compose("unpause", "rabbitmq")
    status, took = wait_for(order, "FULFILLED", 120)
    log(f"broker back → {status} in {took:.0f} s")
    return order


def scenario_worker(log: Callable[[str], None]) -> str:
    """checkout-worker SIGKILLed right after the payment webhook arrives."""
    order = driver({"cmd": "prepare", "sku": SKU})["order_id"]
    driver({"cmd": "pay", "order_id": order})
    compose("kill", "-s", "SIGKILL", "checkout-worker")
    log("payment webhook stored, worker killed with SIGKILL")
    time.sleep(5)
    compose("start", "checkout-worker")
    status, took = wait_for(order, "FULFILLED", 120)
    log(f"worker restarted → {status} in {took:.0f} s")
    return order


def scenario_dependency(log: Callable[[str], None]) -> str:
    """commerce-api down while the stock commit is owed: settlement retries, fulfilment waits for it."""
    order = driver({"cmd": "prepare", "sku": SKU})["order_id"]
    compose("stop", "commerce-api")
    try:
        driver({"cmd": "pay", "order_id": order})
        time.sleep(8)
        state = driver({"cmd": "status", "order_id": order})
        log(f"commerce down → order {state['status']}, settlement owed: {state['settlement']}")
    finally:
        compose("start", "commerce-api")
    status, took = wait_for(order, "FULFILLED", 150)
    log(f"commerce back → {status} in {took:.0f} s")
    return order


def scenario_consumer(log: Callable[[str], None]) -> str:
    """fulfillment-worker down: order.paid.v1 waits in its durable queue, then is processed once."""
    order = driver({"cmd": "prepare", "sku": SKU})["order_id"]
    compose("stop", "fulfillment-worker")
    try:
        driver({"cmd": "pay", "order_id": order})
        time.sleep(8)
        log(f"fulfilment down → {queue_depth('fulfillment.order-paid')} message(s) waiting in the queue")
    finally:
        compose("start", "fulfillment-worker")
    status, took = wait_for(order, "FULFILLED", 120)
    log(f"fulfilment back → {status} in {took:.0f} s")
    return order


def scenario_storm(log: Callable[[str], None]) -> str:
    """The same 'paid' webhook delivered 5×, then a late 'expired': applied once, never undone."""
    order = driver({"cmd": "prepare", "sku": SKU})["order_id"]
    result = driver({"cmd": "pay", "order_id": order, "deliveries": 5, "then_expire": True})
    log(f"webhook responses: {result['webhook_statuses']}")
    status, took = wait_for(order, "FULFILLED", 60)
    log(f"→ {status} in {took:.0f} s")
    return order


def scenario_refund(log: Callable[[str], None]) -> str:
    """The warehouse refuses the order: a real Stripe refund is issued and confirmed by webhook."""
    sku = env_value("FULFILLMENT_MOCK_FAIL_SKUS").split(",")[0]
    order = driver({"cmd": "prepare", "sku": sku})["order_id"]
    driver({"cmd": "pay", "order_id": order})
    status, took = wait_for(order, "REFUNDED", 120)
    log(f"{sku} refused by the warehouse → {status} in {took:.0f} s (Stripe refund confirmed by webhook)")
    return order


SCENARIOS = {
    "broker": scenario_broker,
    "worker": scenario_worker,
    "dependency": scenario_dependency,
    "consumer": scenario_consumer,
    "storm": scenario_storm,
    "refund": scenario_refund,
}
EXPECTED = {"refund": "REFUNDED"}  # every other scenario must end FULFILLED


def fulfillment_jobs(order_ids: list[str]) -> dict[str, int]:
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "-e", f"CHAOS_ORDER_IDS={json.dumps(order_ids)}",
         "fulfillment-worker", "python", "-"],
        cwd=ROOT, input=(ROOT / "scripts" / "chaos" / "fulfillment_jobs.py").read_text(encoding="utf-8"),
        capture_output=True, text=True, encoding="utf-8",
    )  # fmt: skip
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("CHAOS_RESULT "))
    data: dict[str, int] = json.loads(line.removeprefix("CHAOS_RESULT "))
    return data


def main() -> int:
    chosen = sys.argv[1:] or list(SCENARIOS)
    orders: dict[str, str] = {}
    for name in chosen:
        print(f"\n== chaos: {name} — {SCENARIOS[name].__doc__}")
        orders[name] = SCENARIOS[name](lambda message: print(f"   {message}"))

    print("\n== invariants")
    time.sleep(3)  # let the last outbox rows publish
    facts = driver({"cmd": "facts", "order_ids": list(orders.values())})
    jobs = fulfillment_jobs(list(orders.values()))
    failures: list[str] = []
    for name, order_id in orders.items():
        order = facts["orders"][order_id]
        want = EXPECTED.get(name, "FULFILLED")
        checks = {
            want.lower(): order["status"] == want,
            "settled": order["settlement"] is None,
            "one order.paid event": order["order_paid_events"] == 1,
            "one fulfilment job": jobs.get(order_id) == 1,
            "paid once in audit": order["audit"].count("order.paid") == 1,
        }
        failures += [f"{name}: {check}" for check, ok in checks.items() if not ok]
        print(
            f"   {name:<10} {order_id}  " + "  ".join(f"{'✓' if ok else '✗'} {c}" for c, ok in checks.items())
        )
    dead = queue_depth(f"{env_value('EVENTS_DLX')}.q")
    system = {
        "audit hash chain intact": facts["audit_chain_ok"],
        "outbox fully published": facts["unpublished_outbox"] == 0,
        "dead-letter queue empty": dead == 0,
    }
    failures += [check for check, ok in system.items() if not ok]
    print("   " + "  ".join(f"{'✓' if ok else '✗'} {c}" for c, ok in system.items()))
    print(f"\n{'PASSED' if not failures else 'FAILED: ' + '; '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
