"""Demo merchant data (catalog, prices, FX, tax, shipping, promotions).

Business data, not config: in production this comes from the merchant's
catalog feed / admin. Idempotent: safe to run on every deploy of the demo.
Run: ``python -m commerce_svc.seed``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_svc.models import (
    FxRate,
    Inventory,
    PriceBookEntry,
    Product,
    Promotion,
    ShippingRate,
    Sku,
    TaxRate,
)

MERCHANT = "merchant_demo"


@dataclass(frozen=True)
class SeedSku:
    id: str
    label: str
    stock: int
    usd: int
    eur: int | None = None  # explicit price books; NGN/JPY come from FX
    gbp: int | None = None
    compare_usd: int | None = None


@dataclass(frozen=True)
class SeedProduct:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    rating: str
    image_hue: int
    skus: tuple[SeedSku, ...]


PRODUCTS: tuple[SeedProduct, ...] = (
    SeedProduct(
        "prod_trail_gtx", "Trail Runner GTX",
        "Waterproof Gore-Tex trail running shoe with a grippy lug outsole and rock plate. 290 g.",
        ("shoe", "shoes", "running", "trail", "waterproof", "sneaker"), "4.7", 210,
        (
            SeedSku("sku_trail_gtx_09_blk", "Men's 9 · Black", 4, 12900, 11900, 10500, 14900),
            SeedSku("sku_trail_gtx_10_blk", "Men's 10 · Black", 6, 12900, 11900, 10500, 14900),
            SeedSku("sku_trail_gtx_11_blk", "Men's 11 · Black", 0, 12900, 11900, 10500, 14900),
        ),
    ),
    SeedProduct(
        "prod_road_air", "Road Glide Air",
        "Lightweight, cushioned daily trainer for road miles. 245 g, 8 mm drop.",
        ("shoe", "shoes", "running", "road", "sneaker"), "4.5", 160,
        (
            SeedSku("sku_road_air_10_wht", "Men's 10 · White", 8, 11000, 9900, 8900),
            SeedSku("sku_road_air_08_wht", "Women's 8 · White", 5, 11000, 9900, 8900),
        ),
    ),
    SeedProduct(
        "prod_summit_ltd", "Summit Pro Limited",
        "Limited-run carbon-plated racer for race day.",
        ("shoe", "shoes", "running", "race", "limited", "carbon"), "4.9", 20,
        (SeedSku("sku_summit_ltd_10_org", "Men's 10 · Orange", 2, 18900, 17500, 15500),),
    ),
    SeedProduct(
        "prod_merino_socks", "Merino Run Socks (2-pack)",
        "Breathable merino-blend running socks with blister-resistant seams.",
        ("sock", "socks", "running", "accessory"), "4.8", 280,
        (
            SeedSku("sku_merino_socks_m", "Size M", 40, 2400, 2200, 1900),
            SeedSku("sku_merino_socks_l", "Size L", 25, 2400, 2200, 1900),
        ),
    ),
    SeedProduct(
        "prod_storm_shell", "Storm Shell Jacket",
        "Packable 2.5-layer waterproof rain jacket with taped seams.",
        ("jacket", "rain", "coat", "waterproof", "outerwear"), "4.6", 230,
        (SeedSku("sku_storm_shell_m_navy", "M · Navy", 7, 16500, 14900, 12900),),
    ),
    SeedProduct(
        "prod_feather_cap", "Featherlight Cap",
        "Ultralight running cap with a sweat-wicking band.",
        ("cap", "hat", "running", "accessory"), "4.3", 45,
        (SeedSku("sku_feather_cap_os", "One size", 0, 3200, 2900, 2500),),
    ),
)  # fmt: skip

# (base, quote, rate): 1 USD = rate units of quote. Used only where no explicit price exists.
FX: tuple[tuple[str, str, str], ...] = (("USD", "NGN", "1550.00"), ("USD", "JPY", "147.50"))

# (country, region, bps, inclusive)
TAX: tuple[tuple[str, str | None, int, bool], ...] = (
    ("US", "TX", 825, False),
    ("US", "CA", 725, False),
    ("US", "NY", 400, False),
    ("US", None, 0, False),
    ("GB", None, 2000, True),
    ("DE", None, 1900, True),
    ("NG", None, 750, True),
    ("JP", None, 1000, True),
)

# currency → (flat, free_from)
SHIPPING: dict[str, tuple[int, int | None]] = {
    "USD": (800, 10000),
    "EUR": (700, 9000),
    "GBP": (600, 8000),
    "NGN": (500000, 15000000),
    "JPY": (1000, 15000),
}


def image_url(label: str, hue: int) -> str:
    """Placeholder product art (a real catalog would carry CDN URLs)."""
    return f"https://placehold.co/400x400/{_hsl_hex(hue)}/ffffff/png?text={label}"


def _hsl_hex(hue: int) -> str:
    import colorsys

    r, g, b = colorsys.hls_to_rgb(hue / 360, 0.45, 0.6)
    return f"{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


async def seed(session: AsyncSession) -> None:
    now = datetime.now(UTC)

    for p in PRODUCTS:
        search_text = " ".join([p.name, p.description, *p.tags])
        initials = "".join(w[0] for w in p.name.split()[:2]).upper()
        await session.execute(
            insert(Product)
            .values(
                id=p.id, merchant_id=MERCHANT, name=p.name, description=p.description, tags=list(p.tags),
                rating=Decimal(p.rating), active=True, search_text=search_text,
            )
            .on_conflict_do_update(
                index_elements=[Product.id],
                set_={"name": p.name, "description": p.description, "tags": list(p.tags), "search_text": search_text},
            )
        )  # fmt: skip
        for position, s in enumerate(p.skus):
            await session.execute(
                insert(Sku)
                .values(id=s.id, product_id=p.id, variant_label=s.label, image_url=image_url(initials, p.image_hue),
                        position=position, active=True)
                .on_conflict_do_update(index_elements=[Sku.id], set_={"variant_label": s.label, "position": position})
            )  # fmt: skip
            for currency, amount, compare in (
                ("USD", s.usd, s.compare_usd),
                ("EUR", s.eur, None),
                ("GBP", s.gbp, None),
            ):
                if amount is None:
                    continue
                await session.execute(
                    insert(PriceBookEntry)
                    .values(sku_id=s.id, currency=currency, amount_minor=amount, compare_at_minor=compare)
                    .on_conflict_do_update(
                        index_elements=[PriceBookEntry.sku_id, PriceBookEntry.currency],
                        set_={"amount_minor": amount, "compare_at_minor": compare},
                    )
                )
            await session.execute(
                insert(Inventory)
                .values(sku_id=s.id, on_hand=s.stock, reserved=0)
                .on_conflict_do_update(
                    index_elements=[Inventory.sku_id], set_={"on_hand": s.stock, "reserved": 0}
                )
            )

    # Fresh FX snapshot on every seed (quotes reference the snapshot they used).
    for base, quote, rate in FX:
        session.add(
            FxRate(id=f"fx_{base}_{quote}_{int(now.timestamp())}", base=base, quote=quote,
                   rate=Decimal(rate), fetched_at=now)
        )  # fmt: skip

    await session.execute(delete(TaxRate))
    session.add_all(TaxRate(country=c, region=r, rate_bps=b, inclusive=i) for c, r, b, i in TAX)

    for currency, (flat, free_from) in SHIPPING.items():
        await session.execute(
            insert(ShippingRate)
            .values(currency=currency, flat_minor=flat, free_from_minor=free_from)
            .on_conflict_do_update(
                index_elements=[ShippingRate.currency],
                set_={"flat_minor": flat, "free_from_minor": free_from},
            )
        )

    promotions = (
        dict(id="promo_welcome10", code="WELCOME10", label="Welcome offer", kind="percent", percent_bps=1000,
             advertised=True),
        dict(id="promo_save20", code="SAVE20", label="$20 off $150+", kind="fixed", amount_minor=2000,
             currency="USD", min_subtotal_minor=15000, advertised=True),
        dict(id="promo_bundle5", code=None, label="Bundle deal (5% off $300+)", kind="percent", percent_bps=500,
             currency="USD", min_subtotal_minor=30000, advertised=True),
    )  # fmt: skip
    for promo in promotions:
        await session.execute(
            insert(Promotion)
            .values(merchant_id=MERCHANT, active=True, redemption_count=0, **promo)
            .on_conflict_do_update(
                index_elements=[Promotion.id], set_={"label": promo["label"], "active": True}
            )
        )


async def _main() -> None:
    from commerce_common.db import create_engine, create_session_factory
    from commerce_common.observability import configure_logging
    from commerce_svc.settings import CommerceSettings

    settings = CommerceSettings()
    configure_logging(settings)
    engine = create_engine(settings.database_url, pool_size=1, max_overflow=0)
    async with create_session_factory(engine)() as session, session.begin():
        await seed(session)
    await engine.dispose()
    print("commerce-svc: demo data seeded")


if __name__ == "__main__":
    asyncio.run(_main())
