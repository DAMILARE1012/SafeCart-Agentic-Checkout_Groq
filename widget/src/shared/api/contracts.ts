/**
 * Wire contracts between the widget and gateway-svc (see docs/SYSTEM_DESIGN.md §6.2 and §17).
 *
 * Rule: the widget NEVER computes money. Every amount is rendered exactly as the
 * backend sends it (integer minor units + ISO-4217 currency).
 */

export interface Money {
  amount_minor: number;
  currency: string;
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------
export interface MerchantInfo {
  id: string;
  name: string;
  logo_url?: string | null;
  currency: string;
}

export interface CreateSessionRequest {
  publishable_key: string;
  locale: string;
  currency?: string;
  resume_conversation_id?: string;
}

export interface SessionResponse {
  session_token: string;
  expires_at: string;
  conversation_id: string;
  merchant: MerchantInfo;
}

// ---------------------------------------------------------------------------
// Catalog / cart / quote / order payloads (rendered as UI blocks)
// ---------------------------------------------------------------------------
export interface ProductSummary {
  id: string;
  sku_id: string;
  name: string;
  variant_label?: string | null;
  description?: string | null;
  image_url?: string | null;
  price: Money;
  compare_at_price?: Money | null;
  in_stock: boolean;
  rating?: number | null;
}

export interface CartLine {
  id: string;
  sku_id: string;
  name: string;
  variant_label?: string | null;
  image_url?: string | null;
  quantity: number;
  unit_price: Money;
  line_total: Money;
}

export interface AppliedDiscount {
  code?: string | null;
  label: string;
  amount: Money; // positive number, displayed as a reduction
}

export interface CartSnapshot {
  id: string;
  lines: CartLine[];
  item_count: number;
  subtotal: Money;
  discounts: AppliedDiscount[];
  /** subtotal − discounts, before shipping & tax. Computed by the backend, never here. */
  total_after_discounts?: Money;
}

export interface QuoteLine {
  name: string;
  variant_label?: string | null;
  quantity: number;
  line_total: Money;
}

export interface Quote {
  id: string;
  lines: QuoteLine[];
  subtotal: Money;
  discounts: AppliedDiscount[];
  shipping: Money;
  tax_total: Money;
  tax_inclusive: boolean;
  total: Money;
  expires_at: string;
}

/** Issued by gateway-svc (never by the agent). Single-use, bound to the quote. */
export interface ConfirmationGrant {
  token: string;
  expires_at: string;
}

export type OrderStatus =
  | 'CREATED'
  | 'AWAITING_PAYMENT'
  | 'PAID'
  | 'FULFILLED'
  | 'PAYMENT_FAILED'
  | 'EXPIRED'
  | 'CANCELED'
  | 'FULFILLMENT_FAILED'
  | 'REFUND_PENDING'
  | 'REFUNDED'
  | 'MANUAL_REVIEW';

export interface OrderSummary {
  id: string;
  status: OrderStatus;
  total: Money;
  checkout_url?: string | null;
  refund?: { amount: Money; status: 'pending' | 'succeeded' | 'failed' } | null;
  updated_at: string;
}

// ---------------------------------------------------------------------------
// UI blocks: channel-agnostic structured content emitted by agent-svc `render`
// ---------------------------------------------------------------------------
export type UiBlock =
  | { type: 'product_list'; products: ProductSummary[] }
  | { type: 'product_card'; product: ProductSummary }
  | { type: 'cart'; cart: CartSnapshot }
  | { type: 'quote'; quote: Quote; confirmation: ConfirmationGrant | null }
  | { type: 'order_status'; order: OrderSummary }
  | { type: 'notice'; level: 'info' | 'warning' | 'error'; text: string };

// ---------------------------------------------------------------------------
// Conversation
// ---------------------------------------------------------------------------
/** Structured intent attached to a user turn when it comes from a UI control (not free text). */
export type UserAction =
  | { type: 'add_to_cart'; sku_id: string; quantity: number }
  | { type: 'update_cart_item'; cart_item_id: string; quantity: number }
  | { type: 'remove_cart_item'; cart_item_id: string }
  | { type: 'view_product'; product_id: string }
  | { type: 'start_checkout' }
  | { type: 'refresh_quote' };

export interface TurnRequest {
  client_message_id: string;
  text: string;
  action?: UserAction;
}

export interface HistoryMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  blocks: UiBlock[];
  suggestions?: string[];
  created_at: string;
}

/** SSE events on POST /v1/conversations/{id}/turns */
export type TurnStreamEvent =
  | { event: 'turn.started'; data: { turn_id: string; message_id: string } }
  | { event: 'text.delta'; data: { message_id: string; delta: string } }
  | { event: 'block'; data: { message_id: string; block: UiBlock } }
  | { event: 'suggestions'; data: { message_id: string; suggestions: string[] } }
  | { event: 'turn.completed'; data: { message_id: string } }
  | { event: 'turn.error'; data: ApiErrorBody & { message_id?: string } };

/** SSE events on GET /v1/conversations/{id}/events (server push) */
export type ConversationStreamEvent =
  | { event: 'order.updated'; data: OrderSummary }
  | { event: 'notice'; data: { level: 'info' | 'warning' | 'error'; text: string } };

// ---------------------------------------------------------------------------
// Checkout
// ---------------------------------------------------------------------------
export interface ConfirmCheckoutResponse {
  order_id: string;
  checkout_url: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  retryable: boolean;
}
