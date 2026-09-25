import { ShoppingBag, X } from 'lucide-react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { CountBadge } from '@/shared/ui/CountBadge';
import { IconButton } from '@/shared/ui/IconButton';
import { selectCartCount } from '@/features/cart/cartSlice';
import { AssistantAvatar } from '@/features/chat/AssistantAvatar';
import { panelClosed, viewChanged } from '@/features/ui/uiSlice';

export function PanelHeader({ titleId }: { titleId: string }) {
  const dispatch = useAppDispatch();
  const name = useAppSelector((s) => s.session.merchant?.name ?? s.config.merchantName ?? strings.panelTitle);
  const cartCount = useAppSelector(selectCartCount);
  const view = useAppSelector((s) => s.ui.view);
  const network = useAppSelector((s) => s.connection.network);
  const events = useAppSelector((s) => s.connection.events);

  const status =
    network === 'offline'
      ? { text: strings.statusOffline, dot: 'bg-zinc-400' }
      : events === 'reconnecting'
        ? { text: strings.statusReconnecting, dot: 'bg-amber-500' }
        : { text: strings.statusOnline, dot: 'bg-emerald-500' };

  return (
    <header className="flex items-center gap-3 border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
      <AssistantAvatar className="size-9" />
      <div className="min-w-0 flex-1">
        <h2 id={titleId} className="truncate text-sm font-semibold text-zinc-900 dark:text-zinc-50">
          {name}
        </h2>
        <p className="flex items-center gap-1.5 text-xs text-zinc-500">
          <span className={cn('size-1.5 rounded-full', status.dot)} aria-hidden="true" />
          {status.text}
        </p>
      </div>
      <IconButton
        label={`${strings.viewCart}${cartCount ? ` (${strings.items(cartCount)})` : ''}`}
        aria-pressed={view === 'cart'}
        onClick={() => dispatch(viewChanged(view === 'cart' ? 'chat' : 'cart'))}
      >
        <ShoppingBag className="size-5" aria-hidden="true" />
        <CountBadge count={cartCount} className="bg-brand text-brand-fg" />
      </IconButton>
      <IconButton label={strings.close} onClick={() => dispatch(panelClosed())}>
        <X className="size-5" aria-hidden="true" />
      </IconButton>
    </header>
  );
}
