import { useEffect, useId, useRef } from 'react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import { cn } from '@/shared/lib/cn';
import { CartView } from '@/features/cart/CartView';
import { ChatView } from '@/features/chat/ChatView';
import { ConnectionBanner } from '@/features/connection/ConnectionBanner';
import { panelClosed } from '@/features/ui/uiSlice';
import { PanelHeader } from './PanelHeader';
import { SessionError, SessionLoading } from './SessionStatus';

/**
 * The chat window. Full screen on phones; a floating panel from `sm` up.
 * It's a non-modal dialog: the storefront stays usable behind it on desktop.
 */
export function ChatPanel({ id }: { id: string }) {
  const dispatch = useAppDispatch();
  const titleId = useId();
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const view = useAppSelector((s) => s.ui.view);
  const sessionStatus = useAppSelector((s) => s.session.status);
  const position = useAppSelector((s) => s.config.position);

  // Move focus into the panel when it opens so keyboard users land in the composer.
  useEffect(() => {
    if (sessionStatus === 'ready' && view === 'chat') composerRef.current?.focus();
  }, [sessionStatus, view]);

  return (
    <div
      id={id}
      role="dialog"
      aria-modal="false"
      aria-labelledby={titleId}
      onKeyDown={(e) => {
        if (e.key === 'Escape') dispatch(panelClosed());
      }}
      className={cn(
        'fixed inset-0 z-[2147483646] flex flex-col overflow-hidden bg-white text-zinc-900 motion-safe:animate-panel-in dark:bg-zinc-900 dark:text-zinc-100',
        'sm:inset-auto sm:bottom-24 sm:h-[min(680px,calc(100vh-128px))] sm:w-[400px] sm:rounded-3xl sm:border sm:border-zinc-200 sm:shadow-2xl sm:dark:border-zinc-800',
        position === 'bottom-left' ? 'sm:left-5' : 'sm:right-5',
      )}
    >
      <PanelHeader titleId={titleId} />
      <ConnectionBanner />
      {sessionStatus === 'ready' ? (
        view === 'cart' ? (
          <CartView />
        ) : (
          <ChatView composerRef={composerRef} />
        )
      ) : sessionStatus === 'error' ? (
        <SessionError />
      ) : (
        <SessionLoading />
      )}
    </div>
  );
}
