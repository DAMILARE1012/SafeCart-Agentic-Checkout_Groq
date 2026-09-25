import { Minus, Plus, Trash2 } from 'lucide-react';
import { useAppSelector } from '@/app/hooks';
import type { CartLine } from '@/shared/api/contracts';
import { strings } from '@/shared/i18n/strings';
import { formatMoney } from '@/shared/lib/money';
import { IconButton } from '@/shared/ui/IconButton';
import { ProductImage } from '@/shared/ui/ProductImage';
import { useSendMessage } from '@/features/chat/useSendMessage';

const MAX_QTY = 20;

/**
 * Quantity controls send a turn with an exact action. The line only changes
 * when the backend replies with a new cart snapshot (no optimistic updates to money).
 */
export function CartLineItem({ line }: { line: CartLine }) {
  const locale = useAppSelector((s) => s.config.locale);
  const { send, busy } = useSendMessage();

  const setQuantity = (quantity: number) =>
    send(strings.setQtyMessage(line.name, quantity), { type: 'update_cart_item', cart_item_id: line.id, quantity });

  return (
    <li className="flex gap-3 py-3">
      <ProductImage src={line.image_url} alt={line.name} className="size-16 shrink-0 rounded-xl" />
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium text-zinc-900 dark:text-zinc-100">{line.name}</p>
            {line.variant_label && <p className="text-xs text-zinc-500">{line.variant_label}</p>}
          </div>
          <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">{formatMoney(line.line_total, locale)}</p>
        </div>
        <div className="mt-auto flex items-center justify-between pt-1">
          <div className="flex items-center rounded-full border border-zinc-200 dark:border-zinc-700">
            <IconButton
              label={strings.decreaseQty(line.name)}
              className="size-7"
              disabled={busy || line.quantity <= 1}
              onClick={() => setQuantity(line.quantity - 1)}
            >
              <Minus className="size-3.5" />
            </IconButton>
            <span className="w-6 text-center text-sm tabular-nums" aria-label={`Quantity ${line.quantity}`}>
              {line.quantity}
            </span>
            <IconButton
              label={strings.increaseQty(line.name)}
              className="size-7"
              disabled={busy || line.quantity >= MAX_QTY}
              onClick={() => setQuantity(line.quantity + 1)}
            >
              <Plus className="size-3.5" />
            </IconButton>
          </div>
          <IconButton
            label={strings.removeItem(line.name)}
            className="size-7 hover:text-rose-600"
            disabled={busy}
            onClick={() => send(strings.removeMessage(line.name), { type: 'remove_cart_item', cart_item_id: line.id })}
          >
            <Trash2 className="size-3.5" />
          </IconButton>
        </div>
      </div>
    </li>
  );
}
