"""commerce_db schema. Owned exclusively by commerce-svc (docs P9)."""

from __future__ import annotations

import secrets
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from commerce_common.db import make_metadata
from commerce_common.idempotency import idempotency_table


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class Base(DeclarativeBase):
    metadata = make_metadata()


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class Product(Timestamps, Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String(40)), default=list)
    rating: Mapped[Decimal | None] = mapped_column(Numeric(2, 1))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Maintained by the app (name + description + tags); the tsvector is generated from it.
    search_text: Mapped[str] = mapped_column(Text, default="")
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, search_text)", persisted=True)
    )

    skus: Mapped[list[Sku]] = relationship(back_populates="product", order_by="Sku.position")

    __table_args__ = (
        Index("ix_products_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_products_name_trgm", "name", postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}
        ),
    )


class Sku(Timestamps, Base):
    __tablename__ = "skus"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    variant_label: Mapped[str | None] = mapped_column(String(120))
    image_url: Mapped[str | None] = mapped_column(String(2048))
    position: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped[Product] = relationship(back_populates="skus")
    prices: Mapped[list[PriceBookEntry]] = relationship(lazy="noload")
    inventory: Mapped[Inventory | None] = relationship(lazy="noload")


class PriceBookEntry(Base):
    """Explicit per-currency price. Preferred over FX conversion (docs §6.1)."""

    __tablename__ = "price_book_entries"

    sku_id: Mapped[str] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    compare_at_minor: Mapped[int | None] = mapped_column(BigInteger)

    __table_args__ = (CheckConstraint("amount_minor >= 0", name="amount_non_negative"),)


class Inventory(Base):
    __tablename__ = "inventory"

    sku_id: Mapped[str] = mapped_column(ForeignKey("skus.id", ondelete="CASCADE"), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)  # reservations arrive with checkout (M3)

    __table_args__ = (
        CheckConstraint("on_hand >= 0", name="on_hand_non_negative"),
        CheckConstraint("reserved >= 0 AND reserved <= on_hand", name="reserved_within_on_hand"),
    )

    @property
    def available(self) -> int:
        return self.on_hand - self.reserved


class FxRate(Base):
    """Rate snapshots. Quotes reference the snapshot they used, so they're reproducible."""

    __tablename__ = "fx_rates"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    base: Mapped[str] = mapped_column(String(3))
    quote: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("rate > 0", name="rate_positive"),
        Index("ix_fx_rates_pair_time", "base", "quote", "fetched_at"),
    )


# ---------------------------------------------------------------------------
# Tax, shipping, promotions (business DATA, not config)
# ---------------------------------------------------------------------------
class TaxRate(Base):
    __tablename__ = "tax_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    country: Mapped[str] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(String(10))
    rate_bps: Mapped[int] = mapped_column(Integer)
    inclusive: Mapped[bool] = mapped_column(Boolean)

    __table_args__ = (
        UniqueConstraint("country", "region", postgresql_nulls_not_distinct=True),
        CheckConstraint("rate_bps >= 0 AND rate_bps <= 10000", name="rate_bps_range"),
    )


class ShippingRate(Base):
    __tablename__ = "shipping_rates"

    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    flat_minor: Mapped[int] = mapped_column(BigInteger)
    free_from_minor: Mapped[int | None] = mapped_column(BigInteger)


class Promotion(Timestamps, Base):
    __tablename__ = "promotions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    code: Mapped[str | None] = mapped_column(String(40))  # NULL → automatic promotion
    label: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(10))  # 'percent' | 'fixed'
    percent_bps: Mapped[int | None] = mapped_column(Integer)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    # Currency of amount_minor / min_subtotal_minor. Percent promos without a minimum apply in any currency.
    currency: Mapped[str | None] = mapped_column(String(3))
    min_subtotal_minor: Mapped[int | None] = mapped_column(BigInteger)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_redemptions: Mapped[int | None] = mapped_column(Integer)
    redemption_count: Mapped[int] = mapped_column(Integer, default=0)  # finalised on order.paid (M4)
    advertised: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = (
        UniqueConstraint("merchant_id", "code"),
        CheckConstraint("kind IN ('percent', 'fixed')", name="kind_valid"),
        CheckConstraint(
            "(kind = 'percent' AND percent_bps BETWEEN 1 AND 10000) OR "
            "(kind = 'fixed' AND amount_minor > 0 AND currency IS NOT NULL)",
            name="kind_fields",
        ),
    )


# ---------------------------------------------------------------------------
# Carts & quotes
# ---------------------------------------------------------------------------
class Cart(Timestamps, Base):
    __tablename__ = "carts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(64))
    conversation_id: Mapped[str] = mapped_column(String(64))
    currency: Mapped[str] = mapped_column(String(3))
    promo_code: Mapped[str | None] = mapped_column(String(40))
    # Full address encrypted at rest (Fernet); country/region kept in clear for tax.
    shipping_address_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    ship_country: Mapped[str | None] = mapped_column(String(2))
    ship_region: Mapped[str | None] = mapped_column(String(10))
    # Bumped on every change; quotes remember the version they were built from.
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", order_by="CartItem.created_at", cascade="all, delete-orphan"
    )

    __table_args__ = (UniqueConstraint("merchant_id", "conversation_id"),)


class CartItem(Timestamps, Base):
    __tablename__ = "cart_items"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("skus.id"))
    quantity: Mapped[int] = mapped_column(Integer)

    cart: Mapped[Cart] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("cart_id", "sku_id"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )


class Quote(Timestamps, Base):
    """Immutable, hashed price snapshot (docs §3.6). Only `status`/`locked_order_id` ever change."""

    __tablename__ = "quotes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    cart_id: Mapped[str] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"))
    merchant_id: Mapped[str] = mapped_column(String(64))
    cart_version: Mapped[int] = mapped_column(Integer)
    # active → superseded | expired | locked (confirmed for an order) → released (order failed/expired)
    status: Mapped[str] = mapped_column(String(12), default="active")
    locked_order_id: Mapped[str | None] = mapped_column(String(40), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_minor: Mapped[int] = mapped_column(BigInteger)
    discount_minor: Mapped[int] = mapped_column(BigInteger)
    shipping_minor: Mapped[int] = mapped_column(BigInteger)
    tax_minor: Mapped[int] = mapped_column(BigInteger)
    total_minor: Mapped[int] = mapped_column(BigInteger)
    tax_inclusive: Mapped[bool] = mapped_column(Boolean)
    fx_rate_id: Mapped[str | None] = mapped_column(ForeignKey("fx_rates.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("ix_quotes_cart_status", "cart_id", "status"),
        Index("ix_quotes_status_expires", "status", "expires_at"),
        CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'locked', 'released')", name="status_valid"
        ),
        CheckConstraint("total_minor >= 0", name="total_non_negative"),
    )


class InventoryReservation(Timestamps, Base):
    """Stock held for an order between confirmation and payment (docs §6.2, §7.4)."""

    __tablename__ = "inventory_reservations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    sku_id: Mapped[str] = mapped_column(ForeignKey("skus.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(10), default="reserved")  # reserved|committed|released
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("order_id", "sku_id"),
        Index("ix_inventory_reservations_status_expires", "status", "expires_at"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("status IN ('reserved', 'committed', 'released')", name="status_valid"),
    )


class PromotionRedemption(Timestamps, Base):
    """A promotion use counted against its cap: reserved at confirmation, final on payment."""

    __tablename__ = "promotion_redemptions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(40), index=True)
    promotion_id: Mapped[str] = mapped_column(ForeignKey("promotions.id"))
    status: Mapped[str] = mapped_column(String(10), default="reserved")  # reserved|committed|released

    __table_args__ = (
        UniqueConstraint("order_id", "promotion_id"),
        CheckConstraint("status IN ('reserved', 'committed', 'released')", name="status_valid"),
    )


IDEMPOTENCY_KEYS = idempotency_table(Base.metadata)
