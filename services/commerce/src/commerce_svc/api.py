"""HTTP API (internal: called by agent-svc and checkout-svc, never by browsers)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_common.auth import require_scope
from commerce_common.db import transactional_session
from commerce_common.errors import ValidationFailed
from commerce_common.http import PRINTABLE_PATTERN
from commerce_common.idempotency import fingerprint, idempotency_key_header, run_idempotent
from commerce_common.money import Money, MoneyDTO
from commerce_svc.carts import CartService
from commerce_svc.catalog import CatalogService
from commerce_svc.models import IDEMPOTENCY_KEYS, Promotion
from commerce_svc.pricing.rules import PromotionRepository
from commerce_svc.quotes import QuoteService
from commerce_svc.reservations import ReservationService
from commerce_svc.schemas import (
    AddItemRequest,
    AdvertisedPromotionDTO,
    ApplyPromoRequest,
    CartSnapshotDTO,
    CreateCartRequest,
    ProductSearchResponse,
    ProductSummaryDTO,
    PromotionListResponse,
    QuoteDTO,
    ShippingAddressRequest,
    UpdateItemRequest,
)
from commerce_svc.settings import CommerceSettings

# Caller → scopes (docs §3.4): agent-svc reads and edits carts; checkout-svc reads quotes (locks them in M3).
CALLER_SCOPES: dict[str, frozenset[str]] = {
    "agent-svc": frozenset({"commerce:read", "commerce:cart:write"}),
    "checkout-svc": frozenset({"commerce:read", "commerce:quotes:lock", "commerce:orders:settle"}),
}

READ = Depends(require_scope("commerce:read"))
CART_WRITE = require_scope("commerce:cart:write")


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------
async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async for session in transactional_session(request.app.state.session_factory):
        yield session


def get_settings(request: Request) -> CommerceSettings:
    settings: CommerceSettings = request.app.state.settings
    return settings


Session = Annotated[AsyncSession, Depends(get_session)]
Settings = Annotated[CommerceSettings, Depends(get_settings)]
Caller = Annotated[str, Depends(CART_WRITE)]
IdempotencyKey = Annotated[str, Depends(idempotency_key_header)]


def carts_of(request: Request, session: Session, settings: Settings) -> CartService:
    return CartService(session, settings, request.app.state.address_cipher)


Carts = Annotated[CartService, Depends(carts_of)]


def resolve_currency(settings: CommerceSettings, currency: str | None) -> str:
    currency = (currency or settings.default_currency).upper()
    if currency not in settings.supported_currencies:
        raise ValidationFailed("currency_not_supported", f"{currency} is not supported")
    return currency


async def idempotent(
    session: AsyncSession,
    request: Request,
    *,
    caller: str,
    key: str,
    body: Any,
    handler: Callable[[], Awaitable[Any]],
) -> JSONResponse:
    async def run() -> tuple[int, Any]:
        result = await handler()
        return 200, result.model_dump(mode="json")

    status, payload = await run_idempotent(
        session,
        IDEMPOTENCY_KEYS,
        caller=caller,
        key=key,
        request_fingerprint=fingerprint(request.method, request.url.path, body),
        handler=run,
    )
    return JSONResponse(status_code=status, content=payload)


# ---------------------------------------------------------------------------
# Catalog & promotions
# ---------------------------------------------------------------------------
catalog = APIRouter(prefix="/v1", tags=["catalog"], dependencies=[READ])


@catalog.get("/products/search", response_model=ProductSearchResponse)
async def search_products(
    session: Session,
    settings: Settings,
    q: Annotated[str, Query(max_length=200, pattern=PRINTABLE_PATTERN)] = "",
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
    limit: Annotated[int, Query(ge=1, le=24)] = 8,
    max_price_minor: Annotated[int | None, Query(ge=0)] = None,
    in_stock_only: bool = False,
) -> ProductSearchResponse:
    products = await CatalogService(session, settings).search(
        q,
        currency=resolve_currency(settings, currency),
        limit=limit,
        max_price_minor=max_price_minor,
        in_stock_only=in_stock_only,
    )
    return ProductSearchResponse(products=products)


@catalog.get("/products/{product_id}", response_model=ProductSummaryDTO)
async def get_product(
    product_id: str,
    session: Session,
    settings: Settings,
    currency: Annotated[str | None, Query(pattern=r"^[A-Za-z]{3}$")] = None,
) -> ProductSummaryDTO:
    return await CatalogService(session, settings).get(product_id, resolve_currency(settings, currency))


@catalog.get("/promotions", response_model=PromotionListResponse)
async def list_promotions(session: Session, settings: Settings) -> PromotionListResponse:
    promos = await PromotionRepository(session, settings.default_merchant_id).advertised()
    return PromotionListResponse(
        promotions=[
            AdvertisedPromotionDTO(
                code=p.code,
                label=p.label,
                description=describe(p),
                percent_off=(p.percent_bps / 100) if p.kind == "percent" and p.percent_bps else None,
                amount_off=(
                    MoneyDTO(amount_minor=p.amount_minor, currency=p.currency)
                    if p.kind == "fixed" and p.amount_minor is not None and p.currency
                    else None
                ),
                min_subtotal=(
                    MoneyDTO(amount_minor=p.min_subtotal_minor, currency=p.currency)
                    if p.min_subtotal_minor is not None and p.currency
                    else None
                ),
            )
            for p in promos
        ]
    )


def describe(promo: Promotion) -> str:
    """Plain-language terms the agent can quote verbatim (amounts formatted from minor units)."""
    if promo.kind == "percent":
        text = f"{(promo.percent_bps or 0) / 100:g}% off"
    else:
        text = f"{Money(promo.amount_minor or 0, promo.currency or 'USD')} off"
    if promo.min_subtotal_minor and promo.currency:
        text += f" orders of {Money(promo.min_subtotal_minor, promo.currency)} or more"
    return text if promo.code is None else f"{text} with code {promo.code}"


# ---------------------------------------------------------------------------
# Carts
# ---------------------------------------------------------------------------
carts = APIRouter(prefix="/v1/carts", tags=["carts"])


@carts.post("", response_model=CartSnapshotDTO, dependencies=[Depends(CART_WRITE)])
async def get_or_create_cart(body: CreateCartRequest, service: Carts) -> CartSnapshotDTO:
    """Naturally idempotent: one cart per conversation."""
    cart = await service.get_or_create(body.conversation_id, body.currency)
    return await service.snapshot(cart)


@carts.get("/{cart_id}", response_model=CartSnapshotDTO, dependencies=[READ])
async def get_cart(cart_id: str, service: Carts) -> CartSnapshotDTO:
    return await service.snapshot(await service.load(cart_id))


@carts.post("/{cart_id}/items", response_model=CartSnapshotDTO)
async def add_item(
    cart_id: str,
    body: AddItemRequest,
    caller: Caller,
    key: IdempotencyKey,
    request: Request,
    session: Session,
    service: Carts,
) -> JSONResponse:
    """NOT naturally idempotent (it increments), so an Idempotency-Key is required."""

    async def handler() -> CartSnapshotDTO:
        return await service.snapshot(await service.add_item(cart_id, body.sku_id, body.quantity))

    return await idempotent(session, request, caller=caller, key=key, body=body.model_dump(), handler=handler)


@carts.patch("/{cart_id}/items/{item_id}", response_model=CartSnapshotDTO, dependencies=[Depends(CART_WRITE)])
async def update_item(cart_id: str, item_id: str, body: UpdateItemRequest, service: Carts) -> CartSnapshotDTO:
    return await service.snapshot(await service.set_quantity(cart_id, item_id, body.quantity))


@carts.delete(
    "/{cart_id}/items/{item_id}", response_model=CartSnapshotDTO, dependencies=[Depends(CART_WRITE)]
)
async def remove_item(cart_id: str, item_id: str, service: Carts) -> CartSnapshotDTO:
    return await service.snapshot(await service.remove_item(cart_id, item_id))


@carts.put("/{cart_id}/promotion", response_model=CartSnapshotDTO, dependencies=[Depends(CART_WRITE)])
async def apply_promotion(cart_id: str, body: ApplyPromoRequest, service: Carts) -> CartSnapshotDTO:
    return await service.snapshot(await service.apply_promo(cart_id, body.code))


@carts.delete("/{cart_id}/promotion", response_model=CartSnapshotDTO, dependencies=[Depends(CART_WRITE)])
async def remove_promotion(cart_id: str, service: Carts) -> CartSnapshotDTO:
    return await service.snapshot(await service.remove_promo(cart_id))


@carts.put("/{cart_id}/shipping-address", response_model=CartSnapshotDTO, dependencies=[Depends(CART_WRITE)])
async def set_shipping_address(cart_id: str, body: ShippingAddressRequest, service: Carts) -> CartSnapshotDTO:
    return await service.snapshot(await service.set_shipping_address(cart_id, body))


@carts.post("/{cart_id}/quotes", response_model=QuoteDTO)
async def create_quote(
    cart_id: str,
    caller: Caller,
    key: IdempotencyKey,
    request: Request,
    session: Session,
    settings: Settings,
    service: Carts,
) -> JSONResponse:
    async def handler() -> QuoteDTO:
        return await QuoteService(session, settings, service).create(cart_id)

    return await idempotent(
        session, request, caller=caller, key=key, body={"cart_id": cart_id}, handler=handler
    )


# ---------------------------------------------------------------------------
# Internal settlement API: checkout-svc only (quote lock, stock/promo reservation, commit, release)
# ---------------------------------------------------------------------------
settlement = APIRouter(prefix="/internal", tags=["settlement"])


class LockRequest(BaseModel):
    order_id: str = Field(min_length=1, max_length=40)


@settlement.post(
    "/quotes/{quote_id}/lock",
    response_model=QuoteDTO,
    dependencies=[Depends(require_scope("commerce:quotes:lock"))],
)
async def lock_quote(
    quote_id: str, body: LockRequest, session: Session, settings: Settings, service: Carts
) -> QuoteDTO:
    """Confirm-time: lock the quote to one order and reserve its stock and promotion uses (idempotent)."""
    quote = await ReservationService(session, settings).lock(quote_id, body.order_id)
    return QuoteService.to_dto(quote, await service.load(quote.cart_id))


@settlement.post("/orders/{order_id}/commit", dependencies=[Depends(require_scope("commerce:orders:settle"))])
async def commit_order(order_id: str, session: Session, settings: Settings) -> dict[str, Any]:
    """Payment confirmed: stock leaves inventory, promotion use is final, cart emptied (idempotent)."""
    return await ReservationService(session, settings).commit(order_id)


@settlement.post(
    "/orders/{order_id}/release", dependencies=[Depends(require_scope("commerce:orders:settle"))]
)
async def release_order(order_id: str, session: Session, settings: Settings) -> dict[str, Any]:
    """Payment failed/expired: give stock and promotion uses back (idempotent)."""
    return await ReservationService(session, settings).release(order_id)


@settlement.post("/orders/{order_id}/void", dependencies=[Depends(require_scope("commerce:orders:settle"))])
async def void_order(order_id: str, session: Session, settings: Settings) -> dict[str, Any]:
    """Paid order refunded (compensation): give promotion uses back; stock is not restocked (idempotent)."""
    return await ReservationService(session, settings).void(order_id)


# ---------------------------------------------------------------------------
# Quotes (read by checkout-svc to verify amount/hash/expiry before charging)
# ---------------------------------------------------------------------------
quotes = APIRouter(prefix="/v1/quotes", tags=["quotes"], dependencies=[READ])


@quotes.get("/{quote_id}", response_model=QuoteDTO)
async def get_quote(quote_id: str, session: Session, settings: Settings, service: Carts) -> QuoteDTO:
    return await QuoteService(session, settings, service).get(quote_id)
