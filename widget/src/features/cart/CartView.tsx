import { ArrowLeft, ShoppingBag } from 'lucide-react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { formatDiscount, formatMoney } from '@/shared/lib/money';
import { Button } from '@/shared/ui/Button';
import { useSendMessage } from '@/features/chat/useSendMessage';
import { viewChanged } from '@/features/ui/uiSlice';
import { CartLineItem } from './CartLineItem';
import { selectCart } from './cartSlice';

/** Full cart view inside the panel. Every change goes through the agent, so the chat stays the single source of truth. */
export function CartView() {
  const dispatch = useAppDispatch();
  const cart = useAppSelector(selectCart);
  const locale = useAppSelector((s) => s.config.locale);
  const { send, busy } = useSendMessage();

  const backToChat = () => dispatch(viewChanged('chat'));
  const checkout = () => {
    send(strings.checkoutMessage, { type: 'start_checkout' });
    backToChat();
  };

  return (
    <section aria-label={strings.cartTitle} className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
        <Button variant="ghost" size="sm" onClick={backToChat} icon={<ArrowLeft className="size-4" aria-hidden="true" />}>
          {strings.backToChat}
        </Button>
      </div>

      {!cart || cart.lines.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center text-zinc-500">
          <ShoppingBag className="size-8" aria-hidden="true" />
          <p className="text-sm">{strings.cartEmpty}</p>
        </div>
      ) : (
        <>
          <ul className="cc-scroll min-h-0 flex-1 divide-y divide-zinc-100 overflow-y-auto px-4 dark:divide-zinc-800">
            {cart.lines.map((line) => (
              <CartLineItem key={line.id} line={line} />
            ))}
          </ul>
          <div className="space-y-2 border-t border-zinc-200 p-4 dark:border-zinc-800">
            {cart.discounts.length > 0 && (
              <div className="flex justify-between text-sm text-zinc-600 dark:text-zinc-400">
                <span>{strings.subtotal}</span>
                <span>{formatMoney(cart.subtotal, locale)}</span>
              </div>
            )}
            {cart.discounts.map((d) => (
              <div key={d.label} className="flex justify-between text-sm text-emerald-700 dark:text-emerald-400">
                <span>{d.code ? `${d.label} (${d.code})` : d.label}</span>
                <span>{formatDiscount(d.amount, locale)}</span>
              </div>
            ))}
            <div className="flex justify-between text-base font-semibold text-zinc-900 dark:text-zinc-100">
              <span>{cart.discounts.length > 0 ? strings.totalAfterDiscounts : strings.subtotal}</span>
              <span>{formatMoney(cart.total_after_discounts ?? cart.subtotal, locale)}</span>
            </div>
            <p className="text-xs text-zinc-500">{strings.taxesAtCheckout}</p>
            <Button fullWidth size="lg" disabled={busy} onClick={checkout}>
              {strings.checkout}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}
