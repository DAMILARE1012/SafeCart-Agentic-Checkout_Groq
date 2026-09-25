import type { OrderStatus } from '@/shared/api/contracts';

export type StatusTone = 'progress' | 'success' | 'warning' | 'danger' | 'neutral';

export interface StatusView {
  label: string;
  description: string;
  tone: StatusTone;
  /** Position on the happy-path stepper, or null when the order left the happy path. */
  step: number | null;
}

export const ORDER_STEPS = ['Confirmed', 'Payment', 'Preparing', 'Complete'] as const;

/** Mirrors the order state machine in checkout-svc (docs/SYSTEM_DESIGN.md §7.1). */
export const STATUS_VIEW: Record<OrderStatus, StatusView> = {
  CREATED: { label: 'Confirming order', description: 'Preparing your secure payment page.', tone: 'progress', step: 0 },
  AWAITING_PAYMENT: {
    label: 'Awaiting payment',
    description: 'Complete payment on the secure Stripe page.',
    tone: 'progress',
    step: 1,
  },
  PAID: { label: 'Payment received', description: "Thanks! We're preparing your order.", tone: 'progress', step: 2 },
  FULFILLED: {
    label: 'Order confirmed',
    description: "Your order is on its way. We've emailed your receipt.",
    tone: 'success',
    step: 3,
  },
  PAYMENT_FAILED: { label: 'Payment failed', description: 'Your card was not charged. You can try again.', tone: 'danger', step: null },
  EXPIRED: { label: 'Checkout expired', description: 'The payment page timed out. Nothing was charged.', tone: 'warning', step: null },
  CANCELED: { label: 'Checkout canceled', description: 'Nothing was charged.', tone: 'warning', step: null },
  FULFILLMENT_FAILED: {
    label: "We couldn't fulfil your order",
    description: "Sorry about this. We're issuing a full refund automatically.",
    tone: 'warning',
    step: null,
  },
  REFUND_PENDING: {
    label: 'Refund in progress',
    description: "We couldn't fulfil your order, so we're refunding you in full.",
    tone: 'warning',
    step: null,
  },
  REFUNDED: {
    label: 'Refunded',
    description: 'Your payment was refunded in full. It can take 5–10 business days to appear.',
    tone: 'neutral',
    step: null,
  },
  MANUAL_REVIEW: {
    label: 'Under review',
    description: 'Our team is reviewing your order and will contact you shortly.',
    tone: 'warning',
    step: null,
  },
};

const TERMINAL = new Set<OrderStatus>(['FULFILLED', 'PAYMENT_FAILED', 'EXPIRED', 'CANCELED', 'REFUNDED', 'MANUAL_REVIEW']);

export const isTerminal = (status: OrderStatus | undefined): boolean => !!status && TERMINAL.has(status);
