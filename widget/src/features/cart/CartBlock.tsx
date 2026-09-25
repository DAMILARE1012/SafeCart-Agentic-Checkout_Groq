import { ShoppingBag } from 'lucide-react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import type { CartSnapshot } from '@/shared/api/contracts';
import { strings } from '@/shared/i18n/strings';
import { formatMoney } from '@/shared/lib/money';
import { Button } from '@/shared/ui/Button';
import { Card } from '@/shared/ui/Card';
import { ProductImage } from '@/shared/ui/ProductImage';
import { useSendMessage } from '@/features/chat/useSendMessage';
import { viewChanged } from '@/features/ui/uiSlice';

/** Compact in-chat cart summary. The full, editable cart lives in CartView. */
export function CartBlock({ cart }: { cart: CartSnapshot }) {
  const dispatch = useAppDispatch();
  const locale = useAppSelector((s) => s.config.locale);
  const { send, busy } = useSendMessage();
  const previews = cart.lines.slice(0, 4);

  return (
    <Card className="p-3">
      <div className="flex items-center gap-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">
        <ShoppingBag className="size-4 text-brand" aria-hidden="true" />
        {strings.cartTitle}
        <span className="font-normal text-zinc-500">· {strings.items(cart.item_count)}</span>
      </div>

      {cart.lines.length === 0 ? (
        <p className="mt-2 text-sm text-zinc-500">{strings.cartEmpty}</p>
      ) : (
        <>
          <div className="mt-3 flex -space-x-2" aria-hidden="true">
            {previews.map((line) => (
              <ProductImage
                key={line.id}
                src={line.image_url}
                alt=""
                className="size-10 rounded-lg ring-2 ring-white dark:ring-zinc-800"
              />
            ))}
          </div>
          <div className="mt-3 flex items-center justify-between text-sm">
            <span className="text-zinc-500">
              {cart.discounts.length > 0 ? strings.totalAfterDiscounts : strings.subtotal}
            </span>
            <span className="font-semibold text-zinc-900 dark:text-zinc-100">
              {formatMoney(cart.total_after_discounts ?? cart.subtotal, locale)}
            </span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-2">
            <Button variant="secondary" size="sm" onClick={() => dispatch(viewChanged('cart'))}>
              {strings.viewCart}
            </Button>
            <Button size="sm" disabled={busy} onClick={() => send(strings.checkoutMessage, { type: 'start_checkout' })}>
              {strings.checkout}
            </Button>
          </div>
        </>
      )}
    </Card>
  );
}
