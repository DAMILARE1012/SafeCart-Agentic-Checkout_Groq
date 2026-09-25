import { AlertTriangle, CheckCircle2, ExternalLink, Loader2, RotateCcw, XCircle } from 'lucide-react';
import type { ReactNode } from 'react';
import { useAppSelector } from '@/app/hooks';
import type { OrderSummary } from '@/shared/api/contracts';
import { gatewayApi, useGetOrderQuery } from '@/shared/api/gatewayApi';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { formatMoney } from '@/shared/lib/money';
import { Card } from '@/shared/ui/Card';
import { OrderProgress } from './OrderProgress';
import { isTerminal, STATUS_VIEW, type StatusTone } from './orderStatus';

const POLL_MS = 5_000;

const toneStyles: Record<StatusTone, { icon: ReactNode; className: string }> = {
  progress: { icon: <Loader2 className="size-4 animate-spin" />, className: 'text-brand' },
  success: { icon: <CheckCircle2 className="size-4" />, className: 'text-emerald-600 dark:text-emerald-400' },
  warning: { icon: <AlertTriangle className="size-4" />, className: 'text-amber-600 dark:text-amber-400' },
  danger: { icon: <XCircle className="size-4" />, className: 'text-rose-600 dark:text-rose-400' },
  neutral: { icon: <RotateCcw className="size-4" />, className: 'text-zinc-600 dark:text-zinc-300' },
};

interface OrderTrackerProps {
  orderId: string;
  /** Snapshot from a server block, shown until the first fetch returns. */
  initial?: OrderSummary;
  /** Rendered inside another card (e.g. the quote), so no card chrome of its own. */
  embedded?: boolean;
}

/**
 * Live order status. Server push (order.updated) writes into the RTK Query
 * cache. Polling is the fallback and stops at a terminal state or when the tab
 * is hidden.
 */
export function OrderTracker({ orderId, initial, embedded = false }: OrderTrackerProps) {
  const locale = useAppSelector((s) => s.config.locale);
  // Latest known status (cache first, then the block snapshot) decides whether to keep polling.
  const cachedStatus = useAppSelector((s) => gatewayApi.endpoints.getOrder.select(orderId)(s).data?.status);
  const { data } = useGetOrderQuery(orderId, {
    pollingInterval: isTerminal(cachedStatus ?? initial?.status) ? 0 : POLL_MS,
    skipPollingIfUnfocused: true,
  });
  const order = data ?? initial;

  if (!order) {
    return (
      <div className="flex items-center gap-2 text-sm text-zinc-500">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" /> {strings.orderLabel(orderId.slice(-8))}
      </div>
    );
  }

  const view = STATUS_VIEW[order.status];
  const tone = toneStyles[view.tone];

  const content = (
    <div className="space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div aria-live="polite" className="min-w-0">
          <p className={cn('flex items-center gap-1.5 text-sm font-semibold', tone.className)}>
            <span aria-hidden="true">{tone.icon}</span>
            {view.label}
          </p>
          <p className="mt-0.5 text-xs leading-relaxed text-zinc-500">{view.description}</p>
        </div>
        <span className="shrink-0 text-xs text-zinc-400">{strings.orderLabel(`#${order.id.slice(-8).toUpperCase()}`)}</span>
      </div>

      {view.step !== null && <OrderProgress step={view.step} />}

      {order.refund && (
        <div className="flex justify-between rounded-xl bg-zinc-50 px-3 py-2 text-sm dark:bg-zinc-800">
          <span className="text-zinc-600 dark:text-zinc-400">
            {strings.refundLabel} · {order.refund.status}
          </span>
          <span className="font-medium tabular-nums">{formatMoney(order.refund.amount, locale)}</span>
        </div>
      )}

      {order.status === 'AWAITING_PAYMENT' && order.checkout_url && (
        <a
          href={order.checkout_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-xs font-medium text-brand hover:underline"
        >
          {strings.openPaymentPage} <ExternalLink className="size-3" aria-hidden="true" />
        </a>
      )}
    </div>
  );

  return embedded ? content : <Card className="p-4">{content}</Card>;
}
