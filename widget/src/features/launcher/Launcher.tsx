import { MessageCircle, X } from 'lucide-react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { CountBadge } from '@/shared/ui/CountBadge';
import { panelClosed, panelOpened } from '@/features/ui/uiSlice';

/** Floating button that opens and closes the assistant. Hidden on phones while the full-screen panel is open. */
export function Launcher({ panelId }: { panelId: string }) {
  const dispatch = useAppDispatch();
  const isOpen = useAppSelector((s) => s.ui.isOpen);
  const unread = useAppSelector((s) => s.ui.unread);
  const position = useAppSelector((s) => s.config.position);

  const label = isOpen
    ? strings.launcherClose
    : unread > 0
      ? `${strings.launcherOpen} (${unread} new)`
      : strings.launcherOpen;

  return (
    <button
      type="button"
      aria-label={label}
      aria-expanded={isOpen}
      aria-controls={panelId}
      onClick={() => dispatch(isOpen ? panelClosed() : panelOpened())}
      className={cn(
        'fixed bottom-5 z-[2147483647] flex size-14 cursor-pointer items-center justify-center rounded-full',
        'bg-brand text-brand-fg shadow-lg transition hover:scale-105 hover:shadow-xl active:scale-95',
        'focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-brand motion-reduce:transition-none',
        position === 'bottom-left' ? 'left-5' : 'right-5',
        isOpen && 'max-sm:hidden',
      )}
    >
      {isOpen ? <X className="size-6" aria-hidden="true" /> : <MessageCircle className="size-6" aria-hidden="true" />}
      {!isOpen && <CountBadge count={unread} />}
    </button>
  );
}
