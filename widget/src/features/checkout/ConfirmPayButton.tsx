import { Lock } from 'lucide-react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import type { ConfirmationGrant, Quote } from '@/shared/api/contracts';
import { strings } from '@/shared/i18n/strings';
import { formatMoney } from '@/shared/lib/money';
import { Button } from '@/shared/ui/Button';
import { selectCheckoutAttempt } from './checkoutSlice';
import { confirmAndPay } from './checkoutThunks';
import { openPaymentPlaceholder } from './paymentWindow';

interface ConfirmPayButtonProps {
  quote: Quote;
  confirmation: ConfirmationGrant;
  expired: boolean;
}

/**
 * The explicit user confirmation. The exact amount is on the button, the
 * click is the consent, and the request goes straight to the gateway without
 * passing through the LLM.
 */
export function ConfirmPayButton({ quote, confirmation, expired }: ConfirmPayButtonProps) {
  const dispatch = useAppDispatch();
  const locale = useAppSelector((s) => s.config.locale);
  const attempt = useAppSelector((s) => selectCheckoutAttempt(s, quote.id));
  const confirming = attempt?.status === 'confirming';
  const total = formatMoney(quote.total, locale);

  const onConfirm = () => {
    if (expired || confirming || attempt?.status === 'redirected') return;
    // Must happen synchronously inside the click, or the browser blocks the new tab.
    openPaymentPlaceholder(quote.id);
    void dispatch(confirmAndPay({ quoteId: quote.id, confirmationToken: confirmation.token }));
  };

  return (
    <div className="space-y-2">
      <Button
        fullWidth
        size="lg"
        loading={confirming}
        disabled={expired}
        onClick={onConfirm}
        icon={<Lock className="size-4" aria-hidden="true" />}
      >
        {confirming ? strings.confirming : strings.confirmAndPay(total)}
      </Button>
      {attempt?.status === 'failed' && (
        <p role="alert" className="text-xs text-rose-600 dark:text-rose-400">
          {attempt.error}
        </p>
      )}
      <p className="text-xs leading-relaxed text-zinc-500">{strings.confirmHint}</p>
    </div>
  );
}
