import { ArrowDown } from 'lucide-react';
import { useRef } from 'react';
import { useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { selectMessageIds } from './chatSlice';
import { EmptyState } from './EmptyState';
import { MessageItem } from './MessageItem';
import { useStickToBottom } from './useStickToBottom';

export function MessageList() {
  const ids = useAppSelector(selectMessageIds);
  const containerRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLOListElement>(null);
  const { showJump, scrollToBottom } = useStickToBottom(containerRef, contentRef);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={containerRef} className="cc-scroll flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
        {ids.length === 0 && <EmptyState />}
        {/* Always mounted so the scroll observer stays attached. role="log" announces new
            messages to screen readers without stealing focus. */}
        <ol
          ref={contentRef}
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          className={ids.length === 0 ? 'hidden' : 'flex flex-col gap-4 px-4 py-4'}
        >
          {ids.map((id) => (
            <MessageItem key={id} id={id} />
          ))}
        </ol>
      </div>
      {showJump && (
        <button
          type="button"
          onClick={() => scrollToBottom('smooth')}
          className="absolute bottom-3 left-1/2 flex -translate-x-1/2 cursor-pointer items-center gap-1 rounded-full bg-zinc-900/90 px-3 py-1.5 text-xs font-medium text-white shadow-lg focus-visible:outline-2 focus-visible:outline-brand dark:bg-zinc-100/90 dark:text-zinc-900"
        >
          <ArrowDown className="size-3.5" aria-hidden="true" />
          {strings.jumpToLatest}
        </button>
      )}
    </div>
  );
}
