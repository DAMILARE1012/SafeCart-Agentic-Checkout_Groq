"""Catalog search and product detail, priced in the caller's currency."""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from commerce_common.errors import NotFound
from commerce_common.money import Money, MoneyDTO
from commerce_svc.models import Inventory, Product, Sku
from commerce_svc.pricing.resolver import PriceResolver
from commerce_svc.schemas import ProductSummaryDTO, VariantDTO
from commerce_svc.settings import CommerceSettings

# Full-text (stemmed) plus trigram similarity on the name for typos ("trial runer").
# Two passes: STRICT requires every meaningful term ("waterproof trail shoes" → only waterproof
# trail shoes); if that finds nothing, BROAD accepts any term ("show me running shoes").
_SEARCH_TEMPLATE = """
    WITH q AS (SELECT {tsquery} AS tsq)
    SELECT p.id
    FROM products p, q
    WHERE p.merchant_id = :merchant_id AND p.active
      AND (
        (q.tsq IS NOT NULL AND p.search_vector @@ q.tsq)
        OR similarity(p.name, :q) > 0.3
        OR word_similarity(:q, p.name) > 0.5
      )
    ORDER BY COALESCE(ts_rank(p.search_vector, q.tsq), 0) + similarity(p.name, :q) DESC,
             p.rating DESC NULLS LAST, p.id
    LIMIT :limit
"""
_STRICT_SQL = text(
    _SEARCH_TEMPLATE.format(tsquery="NULLIF(plainto_tsquery('english', :q)::text, '')::tsquery")
)
_BROAD_SQL = text(
    _SEARCH_TEMPLATE.format(
        tsquery="NULLIF(replace(plainto_tsquery('english', :q)::text, '&', '|'), '')::tsquery"
    )
)


class CatalogService:
    def __init__(self, session: AsyncSession, settings: CommerceSettings) -> None:
        self._session = session
        self._settings = settings
        self._merchant_id = settings.default_merchant_id
        self._prices = PriceResolver(session, settings)

    async def search(
        self,
        query: str,
        *,
        currency: str,
        limit: int,
        max_price_minor: int | None = None,
        in_stock_only: bool = False,
    ) -> list[ProductSummaryDTO]:
        # Over-fetch: price/stock filters apply after pricing in the requested currency.
        fetch = limit * 3
        if query.strip():
            params = {"q": query.strip()[:200], "merchant_id": self._merchant_id, "limit": fetch}
            ids = [row.id for row in await self._session.execute(_STRICT_SQL, params)]
            if not ids:
                ids = [row.id for row in await self._session.execute(_BROAD_SQL, params)]
        else:
            stmt = (
                select(Product.id)
                .where(Product.merchant_id == self._merchant_id, Product.active.is_(True))
                .order_by(Product.rating.desc().nulls_last(), Product.id)
                .limit(fetch)
            )
            ids = list((await self._session.execute(stmt)).scalars())

        summaries = await self._summaries(ids, currency)
        results = [
            s
            for s in summaries
            if (not in_stock_only or s.in_stock)
            and (max_price_minor is None or s.price.amount_minor <= max_price_minor)
        ]
        return results[:limit]

    async def get(self, product_id: str, currency: str) -> ProductSummaryDTO:
        summaries = await self._summaries([product_id], currency)
        if not summaries:
            raise NotFound("product_not_found", f"Product {product_id} not found")
        return summaries[0]

    async def _summaries(self, product_ids: list[str], currency: str) -> list[ProductSummaryDTO]:
        if not product_ids:
            return []
        products = {
            p.id: p
            for p in (
                await self._session.execute(
                    select(Product)
                    .where(
                        Product.id.in_(product_ids), Product.merchant_id == self._merchant_id, Product.active
                    )
                    .options(selectinload(Product.skus))
                )
            ).scalars()
        }
        skus = [s for p in products.values() for s in p.skus if s.active]
        sku_ids = [s.id for s in skus]
        prices = await self._prices.resolve(sku_ids, currency)
        stock = {
            inv.sku_id: inv.available
            for inv in (
                await self._session.execute(select(Inventory).where(Inventory.sku_id.in_(sku_ids)))
            ).scalars()
        }

        out: list[ProductSummaryDTO] = []
        for pid in product_ids:  # preserve relevance order
            product = products.get(pid)
            if product is None:
                continue
            variants = [s for s in product.skus if s.active and s.id in prices]
            if not variants:
                continue  # not sellable in this currency
            default = next((s for s in variants if stock.get(s.id, 0) > 0), variants[0])
            price = prices[default.id]
            out.append(
                ProductSummaryDTO(
                    id=product.id,
                    sku_id=default.id,
                    name=product.name,
                    variant_label=default.variant_label,
                    description=product.description,
                    image_url=default.image_url,
                    price=MoneyDTO.of(price.unit),
                    compare_at_price=MoneyDTO.of(price.compare_at) if price.compare_at else None,
                    in_stock=stock.get(default.id, 0) > 0,
                    rating=float(product.rating) if product.rating is not None else None,
                    variants=[_variant(s, prices[s.id].unit, stock.get(s.id, 0)) for s in variants],
                )
            )
        return out


def _variant(sku: Sku, unit: Money, available: int) -> VariantDTO:
    return VariantDTO(
        sku_id=sku.id, variant_label=sku.variant_label, price=MoneyDTO.of(unit), in_stock=available > 0
    )
