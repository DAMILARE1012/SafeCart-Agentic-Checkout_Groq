"""Unit prices per SKU and currency: explicit price book first, FX conversion second (docs §6.1)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_common.money import Money
from commerce_svc.models import FxRate, PriceBookEntry
from commerce_svc.settings import CommerceSettings


@dataclass(frozen=True, slots=True)
class ResolvedPrice:
    unit: Money
    compare_at: Money | None
    fx_rate_id: str | None  # set when the price was converted, so quotes can reference the snapshot


class PriceResolver:
    def __init__(self, session: AsyncSession, settings: CommerceSettings) -> None:
        self._session = session
        self._settings = settings

    async def resolve(self, sku_ids: Iterable[str], currency: str) -> dict[str, ResolvedPrice]:
        """Prices for every SKU that can be priced in ``currency``. Unpriceable SKUs are omitted."""
        ids = list(dict.fromkeys(sku_ids))
        if not ids:
            return {}

        explicit = await self._entries(ids, currency)
        prices = {
            e.sku_id: ResolvedPrice(
                Money(e.amount_minor, currency),
                Money(e.compare_at_minor, currency) if e.compare_at_minor is not None else None,
                None,
            )
            for e in explicit
        }

        missing = [i for i in ids if i not in prices]
        base_currency = self._settings.default_currency
        if missing and self._settings.pricing_fx_fallback_enabled and currency != base_currency:
            rate = await self.latest_rate(base_currency, currency)
            if rate is not None:
                for e in await self._entries(missing, base_currency):
                    compare = (
                        Money(e.compare_at_minor, base_currency).convert(rate.rate, currency)
                        if e.compare_at_minor is not None
                        else None
                    )
                    prices[e.sku_id] = ResolvedPrice(
                        Money(e.amount_minor, base_currency).convert(rate.rate, currency), compare, rate.id
                    )
        return prices

    async def latest_rate(self, base: str, quote: str) -> FxRate | None:
        """Most recent snapshot, refused if older than FX_MAX_RATE_AGE_MINUTES (stale FX is worse)."""
        oldest = datetime.now(UTC) - timedelta(minutes=self._settings.fx_max_rate_age_minutes)
        stmt = (
            select(FxRate)
            .where(FxRate.base == base, FxRate.quote == quote, FxRate.fetched_at >= oldest)
            .order_by(FxRate.fetched_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _entries(self, sku_ids: list[str], currency: str) -> list[PriceBookEntry]:
        stmt = select(PriceBookEntry).where(
            PriceBookEntry.sku_id.in_(sku_ids), PriceBookEntry.currency == currency
        )
        return list((await self._session.execute(stmt)).scalars())
