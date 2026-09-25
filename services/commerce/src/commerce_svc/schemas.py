"""Public API DTOs. Field names match the widget/gateway contracts (widget/src/shared/api/contracts.ts)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from commerce_common.http import ID_PATTERN, PRINTABLE_PATTERN
from commerce_common.money import MoneyDTO


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class VariantDTO(_Out):
    sku_id: str
    variant_label: str | None
    price: MoneyDTO
    in_stock: bool


class ProductSummaryDTO(_Out):
    id: str
    sku_id: str  # default variant
    name: str
    variant_label: str | None
    description: str | None
    image_url: str | None
    price: MoneyDTO
    compare_at_price: MoneyDTO | None
    in_stock: bool
    rating: float | None
    variants: list[VariantDTO]


class ProductSearchResponse(_Out):
    products: list[ProductSummaryDTO]


# ---------------------------------------------------------------------------
# Promotions
# ---------------------------------------------------------------------------
class AdvertisedPromotionDTO(_Out):
    code: str | None
    label: str
    description: str
    # Structured terms: callers must never have to parse amounts out of prose.
    percent_off: float | None
    amount_off: MoneyDTO | None
    min_subtotal: MoneyDTO | None


class PromotionListResponse(_Out):
    promotions: list[AdvertisedPromotionDTO]


# ---------------------------------------------------------------------------
# Carts
# ---------------------------------------------------------------------------
class CreateCartRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=64, pattern=ID_PATTERN)
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")


class AddItemRequest(BaseModel):
    sku_id: str = Field(min_length=1, max_length=40, pattern=ID_PATTERN)
    quantity: int = Field(default=1, ge=1)


class UpdateItemRequest(BaseModel):
    quantity: int = Field(ge=0, description="0 removes the line")


class ApplyPromoRequest(BaseModel):
    code: str = Field(
        min_length=1, max_length=40, pattern=PRINTABLE_PATTERN
    )  # typos get a friendly 'no such code'


class ShippingAddressRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    line1: str = Field(min_length=1, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=120)
    region: str | None = Field(default=None, max_length=10, description="State/province code, e.g. TX")
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")


class DestinationDTO(_Out):
    country: str
    region: str | None


class CartLineDTO(_Out):
    id: str
    sku_id: str
    product_id: str
    name: str
    variant_label: str | None
    image_url: str | None
    quantity: int
    unit_price: MoneyDTO
    line_total: MoneyDTO


class DiscountDTO(_Out):
    code: str | None
    label: str
    amount: MoneyDTO


class PromoIssueDTO(_Out):
    code: str
    reason: str
    message: str


class CartSnapshotDTO(_Out):
    id: str
    conversation_id: str
    currency: str
    version: int
    lines: list[CartLineDTO]
    item_count: int
    subtotal: MoneyDTO
    discounts: list[DiscountDTO]
    discount_total: MoneyDTO
    total_after_discounts: (
        MoneyDTO  # subtotal − discounts, before shipping & tax (computed here, never by callers)
    )
    promo_code: str | None
    promo_issue: PromoIssueDTO | None
    shipping_destination: DestinationDTO | None


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
class QuoteLineDTO(_Out):
    sku_id: str
    name: str
    variant_label: str | None
    quantity: int
    unit_price: MoneyDTO
    line_total: MoneyDTO
    discount: MoneyDTO
    tax: MoneyDTO


class QuoteDTO(_Out):
    id: str
    cart_id: str
    cart_version: int
    status: str
    valid: bool
    invalid_reason: str | None
    currency: str
    lines: list[QuoteLineDTO]
    subtotal: MoneyDTO
    discounts: list[DiscountDTO]
    shipping: MoneyDTO
    tax_total: MoneyDTO
    tax_inclusive: bool
    total: MoneyDTO
    fx_rate_id: str | None
    hash: str
    expires_at: datetime
