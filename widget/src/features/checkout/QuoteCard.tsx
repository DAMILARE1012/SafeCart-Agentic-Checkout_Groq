import { Clock, ExternalLink, ReceiptText, ShieldCheck } from 'lucide-react';
import { useAppSelector } from '@/app/hooks';
import type { ConfirmationGrant, Quote } from '@/shared/api/contracts';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { formatDiscount, formatMoney } from '@/shared/lib/money';
import { Button } from '@/shared/ui/Button';
import { Card } from '@/shared/ui/Card';
import { useSendMessage } from '@/features/chat/useSendMessage';
import { OrderTracker } from '@/features/orders/OrderTracker';
import { selectCheckoutAttempt } from './checkoutSlice';
import { ConfirmPayButton } from './ConfirmPayButton';
import { SummaryRow } from './SummaryRow';
import { formatCountdown, useCountdown } from './useCountdown';

function earliest(a: string, b?: string | null): string {
  return b && Date.parse(b) < Date.parse(a) ? b : a;
}

/**
 * The binding order summary. Every figure comes from the backend quote (tax,
 * shipping and discounts included), and the price hold is shown as a countdown.
 */
export function QuoteCard({ quote, confirmation }: { quote: Quote; confirmation: ConfirmationGrant | null }) {
  const locale = useAppSelector((s) => s.config.locale);
  const attempt = useAppSelector((s) => selectCheckoutAttempt(s, quote.id));
  const { send, busy } = useSendMessage();
  const secondsLeft = useCountdown(earliest(quote.expires_at, confirmation?.expires_at));
  const expired = secondsLeft === 0;
  const fmt = (m: Parameters<typeof formatMoney>[0]) => formatMoney(m, locale);

  return (
    <Card className="overflow-hidden">
      <header className="flex items-center justify-between border-b border-zinc-100 px-4 py-3 dark:border-zinc-700/80">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          <ReceiptText className="size-4 text-brand" aria-hidden="true" />
          {strings.orderSummary}
        </h3>
        {attempt?.status !== 'redirected' && (
          <span
            className={cn(
              'flex items-center gap-1 text-xs tabular-nums',
              expired ? 'text-rose-600' : secondsLeft < 120 ? 'text-amber-600' : 'text-zinc-500',
            )}
          >
            <Clock className="size-3.5" aria-hidden="true" />
            {expired ? strings.quoteExpired : strings.quoteExpiresIn(formatCountdown(secondsLeft))}
          </span>
        )}
      </header>

      <ul className="space-y-1.5 px-4 pt-3">
        {quote.lines.map((line, i) => (
          <li key={`${line.name}-${i}`} className="flex justify-between gap-3 text-sm text-zinc-800 dark:text-zinc-200">
            <span className="min-w-0">
              <span className="tabular-nums text-zinc-500">{line.quantity}×</span> {line.name}
              {line.variant_label && <span className="text-zinc-500"> · {line.variant_label}</span>}
            </span>
            <span className="tabular-nums">{fmt(line.line_total)}</span>
          </li>
        ))}
      </ul>

      <dl className="mt-3 space-y-1.5 border-t border-dashed border-zinc-200 px-4 pt-3 dark:border-zinc-700">
        <SummaryRow label={strings.subtotal} value={fmt(quote.subtotal)} />
        {quote.discounts.map((d) => (
          <SummaryRow
            key={d.label}
            tone="positive"
            label={d.code ? `${d.label} (${d.code})` : d.label}
            value={formatDiscount(d.amount, locale)}
          />
        ))}
        <SummaryRow
          label={strings.shipping}
          value={quote.shipping.amount_minor === 0 ? strings.free : fmt(quote.shipping)}
        />
        <SummaryRow label={quote.tax_inclusive ? strings.taxIncluded : strings.tax} value={fmt(quote.tax_total)} />
        <div className="pt-1.5">
          <SummaryRow emphasis label={strings.total} value={fmt(quote.total)} />
        </div>
      </dl>

      <div className="space-y-3 p-4">
        {attempt?.status === 'redirected' && attempt.orderId ? (
          <>
            {attempt.popupBlocked && attempt.checkoutUrl ? (
              <a
                href={attempt.checkoutUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="flex h-12 w-full items-center justify-center gap-2 rounded-xl bg-brand text-sm font-medium text-brand-fg hover:brightness-110 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand"
              >
                <ExternalLink className="size-4" aria-hidden="true" />
                {strings.openPaymentPage}
              </a>
            ) : (
              <p className="text-xs text-zinc-500">{strings.paymentPageOpened}</p>
            )}
            <OrderTracker orderId={attempt.orderId} embedded />
          </>
        ) : expired ? (
          <Button fullWidth variant="secondary" disabled={busy} onClick={() => send(strings.refreshQuoteMessage, { type: 'refresh_quote' })}>
            {strings.refreshQuote}
          </Button>
        ) : !confirmation ? (
          // No confirmation grant was issued (e.g. payments not enabled): never offer a pay button.
          <p role="status" className="rounded-xl bg-zinc-50 px-3 py-2 text-center text-xs text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300">
            {strings.paymentUnavailable}
          </p>
        ) : (
          <ConfirmPayButton quote={quote} confirmation={confirmation} expired={expired} />
        )}
        <p className="flex items-center justify-center gap-1.5 text-[11px] text-zinc-400">
          <ShieldCheck className="size-3.5" aria-hidden="true" />
          {strings.poweredBy}
        </p>
      </div>
    </Card>
  );
}
