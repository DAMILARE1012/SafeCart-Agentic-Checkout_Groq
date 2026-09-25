"""End-to-end behaviour of commerce-svc over HTTP against a real, migrated Postgres."""

from __future__ import annotations

from typing import Any

from commerce_testkit import conversation, key
from httpx import AsyncClient

ADDRESS_TX = {"line1": "1 Main St", "city": "Austin", "region": "TX", "postal_code": "78701", "country": "US"}
ADDRESS_GB = {"line1": "10 High St", "city": "London", "postal_code": "SW1A 1AA", "country": "GB"}


async def new_cart(client: AsyncClient, agent: dict[str, str], currency: str = "USD") -> dict[str, Any]:
    r = await client.post(
        "/v1/carts", json={"conversation_id": conversation(), "currency": currency}, headers=agent
    )
    assert r.status_code == 200, r.text
    return r.json()


async def add(
    client: AsyncClient, agent: dict[str, str], cart_id: str, sku: str, qty: int = 1
) -> dict[str, Any]:
    r = await client.post(
        f"/v1/carts/{cart_id}/items", json={"sku_id": sku, "quantity": qty}, headers={**agent, **key()}
    )
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Health & auth
# ---------------------------------------------------------------------------
async def test_readiness_checks_the_database(client: AsyncClient) -> None:
    r = await client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok"}


async def test_service_auth_and_scopes(
    client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    assert (await client.get("/v1/products/search?q=shoe")).status_code == 401
    bad = {"Authorization": "Bearer not-a-real-key"}
    assert (await client.get("/v1/products/search?q=shoe", headers=bad)).status_code == 401
    # checkout-svc may read, but may not edit carts
    assert (await client.get("/v1/products/search?q=shoe", headers=checkout)).status_code == 200
    r = await client.post("/v1/carts", json={"conversation_id": conversation()}, headers=checkout)
    assert r.status_code == 403
    assert r.json()["code"] == "forbidden"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
async def test_search_understands_natural_language_and_typos(
    client: AsyncClient, agent: dict[str, str]
) -> None:
    r = await client.get("/v1/products/search", params={"q": "show me running shoes"}, headers=agent)
    names = [p["name"] for p in r.json()["products"]]
    assert {"Trail Runner GTX", "Road Glide Air", "Summit Pro Limited"} <= set(names)
    assert "Storm Shell Jacket" not in names

    typo = await client.get("/v1/products/search", params={"q": "trial runer"}, headers=agent)
    assert typo.json()["products"][0]["name"] == "Trail Runner GTX"


async def test_specific_queries_match_all_terms_first(client: AsyncClient, agent: dict[str, str]) -> None:
    r = await client.get(
        "/v1/products/search", params={"q": "waterproof shoes for trail running"}, headers=agent
    )
    assert [p["name"] for p in r.json()["products"]] == ["Trail Runner GTX"]


async def test_default_variant_is_in_stock(client: AsyncClient, agent: dict[str, str]) -> None:
    product = (await client.get("/v1/products/prod_trail_gtx", headers=agent)).json()
    assert product["in_stock"] is True
    assert product["sku_id"] == "sku_trail_gtx_09_blk"
    assert {v["sku_id"]: v["in_stock"] for v in product["variants"]}["sku_trail_gtx_11_blk"] is False


async def test_prices_explicit_first_then_fx(client: AsyncClient, agent: dict[str, str]) -> None:
    def price(currency: str) -> Any:
        return client.get("/v1/products/prod_trail_gtx", params={"currency": currency}, headers=agent)

    assert (await price("EUR")).json()["price"] == {"amount_minor": 11900, "currency": "EUR"}  # explicit
    assert (await price("NGN")).json()["price"] == {
        "amount_minor": 19995000,
        "currency": "NGN",
    }  # $129 × 1550
    assert (await price("JPY")).json()["price"] == {
        "amount_minor": 19028,
        "currency": "JPY",
    }  # 19027.5 → half-up
    assert (await price("CHF")).status_code == 422


# ---------------------------------------------------------------------------
# Carts
# ---------------------------------------------------------------------------
async def test_add_item_is_idempotent(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent)
    url = f"/v1/carts/{cart['id']}/items"
    headers = {**agent, "Idempotency-Key": "retry-me"}
    body = {"sku_id": "sku_trail_gtx_10_blk", "quantity": 1}

    first = await client.post(url, json=body, headers=headers)
    replay = await client.post(url, json=body, headers=headers)  # e.g. the agent retried after a timeout
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.json()["item_count"] == 1  # NOT 2

    reused = await client.post(url, json={**body, "quantity": 2}, headers=headers)
    assert reused.status_code == 422
    assert reused.json()["code"] == "idempotency_key_reused"

    missing = await client.post(url, json=body, headers=agent)
    assert missing.status_code == 400
    assert missing.json()["code"] == "idempotency_key_required"


async def test_stock_and_quantity_rules(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent)
    url = f"/v1/carts/{cart['id']}/items"
    too_many = await client.post(
        url, json={"sku_id": "sku_trail_gtx_10_blk", "quantity": 7}, headers={**agent, **key()}
    )
    assert too_many.status_code == 422
    assert too_many.json()["code"] == "insufficient_stock"
    assert too_many.json()["details"] == {"available": 6}

    sold_out = await client.post(url, json={"sku_id": "sku_feather_cap_os"}, headers={**agent, **key()})
    assert sold_out.json()["code"] == "insufficient_stock"

    unknown = await client.post(url, json={"sku_id": "sku_nope"}, headers={**agent, **key()})
    assert unknown.status_code == 404


async def test_update_and_remove_lines(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent)
    snap = await add(client, agent, cart["id"], "sku_merino_socks_m", 2)
    line = snap["lines"][0]
    assert line["line_total"] == {"amount_minor": 4800, "currency": "USD"}

    r = await client.patch(f"/v1/carts/{cart['id']}/items/{line['id']}", json={"quantity": 3}, headers=agent)
    assert r.json()["subtotal"]["amount_minor"] == 7200
    assert r.json()["version"] > snap["version"]

    r = await client.delete(f"/v1/carts/{cart['id']}/items/{line['id']}", headers=agent)
    assert r.json()["lines"] == []


async def test_promotions(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent)
    await add(client, agent, cart["id"], "sku_merino_socks_m")  # $24.00
    promo = f"/v1/carts/{cart['id']}/promotion"

    r = await client.put(promo, json={"code": "SAVE20"}, headers=agent)
    assert r.status_code == 422
    assert r.json()["code"] == "promo_min_subtotal_not_met"
    assert "150.00 USD" in r.json()["message"]

    assert (await client.put(promo, json={"code": "BOGUS"}, headers=agent)).json()[
        "code"
    ] == "promo_not_found"

    r = await client.put(promo, json={"code": "welcome10"}, headers=agent)  # case-insensitive
    assert r.status_code == 200
    assert r.json()["total_after_discounts"] == {"amount_minor": 2160, "currency": "USD"}
    assert r.json()["discounts"] == [
        {"code": "WELCOME10", "label": "Welcome offer", "amount": {"amount_minor": 240, "currency": "USD"}}
    ]


async def test_automatic_promotion_applies_over_threshold(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent)
    snap = await add(client, agent, cart["id"], "sku_trail_gtx_10_blk", 3)  # $387.00 ≥ $300
    assert snap["discounts"][0]["label"].startswith("Bundle deal")
    assert snap["discount_total"]["amount_minor"] == 1935  # 5 %


async def test_advertised_promotions(client: AsyncClient, agent: dict[str, str]) -> None:
    promos = (await client.get("/v1/promotions", headers=agent)).json()["promotions"]
    descriptions = {p["code"]: p["description"] for p in promos}
    assert descriptions["WELCOME10"] == "10% off with code WELCOME10"
    assert descriptions["SAVE20"] == "20.00 USD off orders of 150.00 USD or more with code SAVE20"
    save20 = next(p for p in promos if p["code"] == "SAVE20")
    assert save20["amount_off"] == {"amount_minor": 2000, "currency": "USD"}
    assert save20["min_subtotal"] == {"amount_minor": 15000, "currency": "USD"}


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
async def test_quote_totals_hash_and_invalidation(
    client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    cart = await new_cart(client, agent)
    await add(client, agent, cart["id"], "sku_trail_gtx_10_blk")
    await client.put(f"/v1/carts/{cart['id']}/promotion", json={"code": "WELCOME10"}, headers=agent)

    no_address = await client.post(f"/v1/carts/{cart['id']}/quotes", headers={**agent, **key()})
    assert no_address.json()["code"] == "shipping_address_required"

    r = await client.put(f"/v1/carts/{cart['id']}/shipping-address", json=ADDRESS_TX, headers=agent)
    assert r.json()["shipping_destination"] == {"country": "US", "region": "TX"}

    quote = (await client.post(f"/v1/carts/{cart['id']}/quotes", headers={**agent, **key()})).json()
    # $129.00 − 10 % = $116.10 → free shipping; 8.25 % TX tax = $9.58; total $125.68
    assert quote["subtotal"]["amount_minor"] == 12900
    assert quote["discounts"][0]["amount"]["amount_minor"] == 1290
    assert quote["shipping"]["amount_minor"] == 0
    assert quote["tax_total"]["amount_minor"] == 958
    assert quote["total"]["amount_minor"] == 12568
    assert quote["valid"] is True
    assert len(quote["hash"]) == 64

    # checkout-svc reads the same quote (read scope)
    fetched = (await client.get(f"/v1/quotes/{quote['id']}", headers=checkout)).json()
    assert fetched["hash"] == quote["hash"]
    assert fetched["total"] == quote["total"]

    # Any cart change invalidates the open quote
    await add(client, agent, cart["id"], "sku_merino_socks_m")
    stale = (await client.get(f"/v1/quotes/{quote['id']}", headers=checkout)).json()
    assert stale["valid"] is False
    assert stale["invalid_reason"] == "superseded"


async def test_quote_creation_is_idempotent(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent)
    await add(client, agent, cart["id"], "sku_road_air_10_wht")
    await client.put(f"/v1/carts/{cart['id']}/shipping-address", json=ADDRESS_TX, headers=agent)
    headers = {**agent, **key()}
    first = (await client.post(f"/v1/carts/{cart['id']}/quotes", headers=headers)).json()
    again = (await client.post(f"/v1/carts/{cart['id']}/quotes", headers=headers)).json()
    assert first["id"] == again["id"]


async def test_inclusive_vat_quote(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent, currency="GBP")
    await add(client, agent, cart["id"], "sku_trail_gtx_10_blk")  # £105.00 incl. VAT
    await client.put(f"/v1/carts/{cart['id']}/shipping-address", json=ADDRESS_GB, headers=agent)
    quote = (await client.post(f"/v1/carts/{cart['id']}/quotes", headers={**agent, **key()})).json()
    assert quote["tax_inclusive"] is True
    assert quote["tax_total"]["amount_minor"] == 1750  # 10500 × 20/120
    assert quote["total"]["amount_minor"] == 10500  # VAT already included, free shipping ≥ £80


async def test_fx_priced_quote_references_rate_snapshot(client: AsyncClient, agent: dict[str, str]) -> None:
    cart = await new_cart(client, agent, currency="NGN")
    await add(client, agent, cart["id"], "sku_merino_socks_m")
    await client.put(
        f"/v1/carts/{cart['id']}/shipping-address",
        json={"line1": "1 Marina", "city": "Lagos", "postal_code": "101001", "country": "NG"},
        headers=agent,
    )
    quote = (await client.post(f"/v1/carts/{cart['id']}/quotes", headers={**agent, **key()})).json()
    assert quote["fx_rate_id"] is not None
    assert quote["currency"] == "NGN"


# ---------------------------------------------------------------------------
# Settlement (checkout-svc only): lock → commit | release
# ---------------------------------------------------------------------------
async def quoted_cart(
    client: AsyncClient, agent: dict[str, str], sku: str, qty: int = 1, promo: str | None = None
) -> dict[str, Any]:
    cart = await new_cart(client, agent)
    await add(client, agent, cart["id"], sku, qty)
    if promo:
        await client.put(f"/v1/carts/{cart['id']}/promotion", json={"code": promo}, headers=agent)
    await client.put(f"/v1/carts/{cart['id']}/shipping-address", json=ADDRESS_TX, headers=agent)
    quote = (await client.post(f"/v1/carts/{cart['id']}/quotes", headers={**agent, **key()})).json()
    return {"cart": cart, "quote": quote}


async def test_lock_is_scoped_to_checkout_and_idempotent(
    client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    q = (await quoted_cart(client, agent, "sku_road_air_08_wht"))["quote"]
    url = f"/internal/quotes/{q['id']}/lock"
    assert (await client.post(url, json={"order_id": "ord_a"}, headers=agent)).status_code == 403

    first = await client.post(url, json={"order_id": "ord_a"}, headers=checkout)
    again = await client.post(url, json={"order_id": "ord_a"}, headers=checkout)
    other = await client.post(url, json={"order_id": "ord_b"}, headers=checkout)
    assert first.status_code == again.status_code == 200
    assert first.json()["status"] == "locked"
    assert first.json()["hash"] == q["hash"]
    assert other.status_code == 409 and other.json()["code"] == "quote_already_used"


async def test_lock_reserves_last_units_then_commit_empties_cart(
    client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    # Summit Pro Limited: 2 in stock. Two customers each quote 2; only the first to confirm gets them.
    first = await quoted_cart(client, agent, "sku_summit_ltd_10_org", 2)
    second = await quoted_cart(client, agent, "sku_summit_ltd_10_org", 2)
    ok = await client.post(
        f"/internal/quotes/{first['quote']['id']}/lock", json={"order_id": "ord_s1"}, headers=checkout
    )
    sold_out = await client.post(
        f"/internal/quotes/{second['quote']['id']}/lock", json={"order_id": "ord_s2"}, headers=checkout
    )
    assert ok.status_code == 200
    assert sold_out.status_code == 409 and sold_out.json()["code"] == "insufficient_stock"

    committed = await client.post("/internal/orders/ord_s1/commit", headers=checkout)
    assert committed.json() == {"order_id": "ord_s1", "committed": True, "oversold_sku_ids": []}
    assert (
        await client.post("/internal/orders/ord_s1/commit", headers=checkout)
    ).status_code == 200  # idempotent
    cart = (await client.get(f"/v1/carts/{first['cart']['id']}", headers=agent)).json()
    assert cart["lines"] == []  # the paid order owns those items now
    product = (await client.get("/v1/products/prod_summit_ltd", headers=agent)).json()
    assert product["in_stock"] is False


async def test_release_returns_stock_and_promo_use(
    client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    data = await quoted_cart(client, agent, "sku_storm_shell_m_navy", 7, promo="WELCOME10")  # all 7 in stock
    # 7 × $165 also qualifies for the automatic bundle deal: two promotion uses get reserved
    assert {d["code"] for d in data["quote"]["discounts"]} == {None, "WELCOME10"}
    lock = await client.post(
        f"/internal/quotes/{data['quote']['id']}/lock", json={"order_id": "ord_r1"}, headers=checkout
    )
    assert lock.status_code == 200
    assert (await client.get("/v1/products/prod_storm_shell", headers=agent)).json()["in_stock"] is False

    released = await client.post("/internal/orders/ord_r1/release", headers=checkout)
    assert released.json()["released_reservations"] == 1
    assert (await client.post("/internal/orders/ord_r1/release", headers=checkout)).json()[
        "released_reservations"
    ] == 0
    assert (await client.get("/v1/products/prod_storm_shell", headers=agent)).json()["in_stock"] is True
    quote = (await client.get(f"/v1/quotes/{data['quote']['id']}", headers=checkout)).json()
    assert quote["status"] == "released" and quote["valid"] is False


async def test_stale_or_changed_quotes_cannot_be_locked(
    client: AsyncClient, agent: dict[str, str], checkout: dict[str, str]
) -> None:
    data = await quoted_cart(client, agent, "sku_merino_socks_l")
    await add(client, agent, data["cart"]["id"], "sku_merino_socks_l")  # cart changed → quote superseded
    r = await client.post(
        f"/internal/quotes/{data['quote']['id']}/lock", json={"order_id": "ord_x"}, headers=checkout
    )
    assert r.status_code == 409 and r.json()["code"] == "quote_invalid"
