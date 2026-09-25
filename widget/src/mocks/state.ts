/**
 * In-memory stand-in for commerce-svc + checkout-svc, used ONLY by the dev
 * playground. (Money maths lives here because this simulates the backend;
 * the widget itself never computes money.)
 */
import type {
  CartSnapshot,
  ConfirmationGrant,
  HistoryMessage,
  OrderStatus,
  OrderSummary,
  Quote,
} from '@/shared/api/contracts';
import { FLAT_SHIPPING, FREE_SHIPPING_FROM, PRODUCTS, PROMO_CODES, TAX_RATE, type MockProduct } from './fixtures';

const usd = (amount_minor: number) => ({ amount_minor, currency: 'USD' });
const rid = (prefix: string) => `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
const inMinutes = (m: number) => new Date(Date.now() + m * 60_000).toISOString();

interface CartEntry {
  lineId: string;
  product: MockProduct;
  quantity: number;
}

interface StoredQuote {
  quote: Quote;
  token: string;
  tokenExpiresAt: string;
  usedWithKey: string | null;
  failsFulfillment: boolean;
}

export const db = {
  cart: new Map<string, CartEntry>(), // keyed by sku_id
  promo: null as string | null,
  quotes: new Map<string, StoredQuote>(),
  orders: new Map<string, OrderSummary & { failsFulfillment: boolean }>(),
  idempotentConfirms: new Map<string, { order_id: string; checkout_url: string }>(),
  history: [] as HistoryMessage[],
  lastOrderId: null as string | null,
};

export const findProduct = (skuOrId: string) => PRODUCTS.find((p) => p.sku_id === skuOrId || p.id === skuOrId);

// ---------------------------------------------------------------------------
// Cart
// ---------------------------------------------------------------------------
function discountFor(subtotal: number) {
  const promo = db.promo ? PROMO_CODES[db.promo] : undefined;
  if (!promo || !db.promo) return [];
  return [{ code: db.promo, label: promo.label, amount: usd(Math.round((subtotal * promo.percentOff) / 100)) }];
}

export function cartSnapshot(): CartSnapshot {
  const lines = [...db.cart.values()].map((e) => ({
    id: e.lineId,
    sku_id: e.product.sku_id,
    name: e.product.name,
    variant_label: e.product.variant_label ?? null,
    image_url: e.product.image_url ?? null,
    quantity: e.quantity,
    unit_price: e.product.price,
    line_total: usd(e.product.price.amount_minor * e.quantity),
  }));
  const subtotal = lines.reduce((sum, l) => sum + l.line_total.amount_minor, 0);
  const discounts = discountFor(subtotal);
  return {
    id: 'cart_demo',
    lines,
    item_count: lines.reduce((n, l) => n + l.quantity, 0),
    subtotal: usd(subtotal),
    discounts,
    total_after_discounts: usd(subtotal - discounts.reduce((s, d) => s + d.amount.amount_minor, 0)),
  };
}

export function addToCart(skuId: string, quantity: number): MockProduct | null {
  const product = findProduct(skuId);
  if (!product || !product.in_stock) return null;
  const entry = db.cart.get(product.sku_id);
  if (entry) entry.quantity = Math.min(20, entry.quantity + quantity);
  else db.cart.set(product.sku_id, { lineId: rid('line'), product, quantity });
  return product;
}

export function setQuantity(lineId: string, quantity: number): MockProduct | null {
  for (const [sku, entry] of db.cart) {
    if (entry.lineId !== lineId) continue;
    if (quantity <= 0) db.cart.delete(sku);
    else entry.quantity = Math.min(20, quantity);
    return entry.product;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Quote + confirmation
// ---------------------------------------------------------------------------
export function createQuote(): { quote: Quote; confirmation: ConfirmationGrant } | null {
  const cart = cartSnapshot();
  if (cart.lines.length === 0) return null;
  const subtotal = cart.subtotal.amount_minor;
  const discount = cart.discounts.reduce((s, d) => s + d.amount.amount_minor, 0);
  const shipping = subtotal - discount >= FREE_SHIPPING_FROM ? 0 : FLAT_SHIPPING;
  const tax = Math.round((subtotal - discount) * TAX_RATE);

  const quote: Quote = {
    id: rid('quote'),
    lines: cart.lines.map((l) => ({
      name: l.name,
      variant_label: l.variant_label ?? null,
      quantity: l.quantity,
      line_total: l.line_total,
    })),
    subtotal: cart.subtotal,
    discounts: cart.discounts,
    shipping: usd(shipping),
    tax_total: usd(tax),
    tax_inclusive: false,
    total: usd(subtotal - discount + shipping + tax),
    expires_at: inMinutes(15),
  };
  const confirmation: ConfirmationGrant = { token: rid('cft'), expires_at: inMinutes(10) };
  db.quotes.set(quote.id, {
    quote,
    token: confirmation.token,
    tokenExpiresAt: confirmation.expires_at,
    usedWithKey: null,
    failsFulfillment: [...db.cart.values()].some((e) => e.product.failsFulfillment),
  });
  return { quote, confirmation };
}

export type ConfirmResult =
  | { ok: true; order_id: string; checkout_url: string }
  | { ok: false; status: number; code: string; message: string; retryable: boolean };

/** Single-use token + idempotency key semantics, as in checkout-svc (§6.2, §8). */
export function confirm(token: string, idempotencyKey: string): ConfirmResult {
  const replay = db.idempotentConfirms.get(idempotencyKey);
  if (replay) return { ok: true, ...replay };

  const stored = [...db.quotes.values()].find((q) => q.token === token);
  if (!stored) return { ok: false, status: 404, code: 'confirmation_not_found', message: 'This checkout link is no longer valid.', retryable: false };
  if (stored.usedWithKey) return { ok: false, status: 409, code: 'confirmation_used', message: 'This order was already confirmed.', retryable: false };
  if (Date.parse(stored.tokenExpiresAt) < Date.now()) {
    return { ok: false, status: 410, code: 'confirmation_expired', message: 'This price has expired. Please refresh it.', retryable: false };
  }

  stored.usedWithKey = idempotencyKey;
  const orderId = rid('ord');
  const checkoutUrl = `${window.location.origin}/mock-checkout.html?order=${orderId}&amount=${stored.quote.total.amount_minor}&currency=${stored.quote.total.currency}`;
  db.orders.set(orderId, {
    id: orderId,
    status: 'AWAITING_PAYMENT',
    total: stored.quote.total,
    checkout_url: checkoutUrl,
    refund: null,
    updated_at: new Date().toISOString(),
    failsFulfillment: stored.failsFulfillment,
  });
  db.lastOrderId = orderId;
  const result = { order_id: orderId, checkout_url: checkoutUrl };
  db.idempotentConfirms.set(idempotencyKey, result);
  return { ok: true, ...result };
}

// ---------------------------------------------------------------------------
// Orders
// ---------------------------------------------------------------------------
export function publicOrder(id: string): OrderSummary | null {
  const order = db.orders.get(id);
  if (!order) return null;
  const { failsFulfillment: _ignored, ...summary } = order;
  return summary;
}

export function transition(id: string, status: OrderStatus, patch: Partial<OrderSummary> = {}): OrderSummary | null {
  const order = db.orders.get(id);
  if (!order) return null;
  Object.assign(order, patch, { status, updated_at: new Date().toISOString() });
  if (status === 'PAID') db.cart.clear();
  return publicOrder(id);
}
