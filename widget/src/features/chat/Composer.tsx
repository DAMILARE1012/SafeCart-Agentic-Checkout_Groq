import { ArrowUp } from 'lucide-react';
import { forwardRef, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { MAX_MESSAGE_LENGTH } from './chatThunks';
import { useSendMessage } from './useSendMessage';

const MAX_HEIGHT_PX = 120;
const COUNTER_FROM = MAX_MESSAGE_LENGTH - 200;

export const Composer = forwardRef<HTMLTextAreaElement>(function Composer(_, forwardedRef) {
  const [text, setText] = useState('');
  const innerRef = useRef<HTMLTextAreaElement | null>(null);
  const { send, busy } = useSendMessage();
  const offline = useAppSelector((s) => s.connection.network === 'offline');
  const canSend = text.trim().length > 0 && !busy && !offline;

  // Grow with the content up to a max height, then scroll.
  useLayoutEffect(() => {
    const el = innerRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [text]);

  const submit = () => {
    if (!canSend) return;
    send(text);
    setText('');
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter adds a newline; never send mid-IME composition (CJK input).
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form
      className="border-t border-zinc-200 p-3 dark:border-zinc-800"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div className="flex items-end gap-2 rounded-2xl border border-zinc-200 bg-white px-3 py-2 transition focus-within:border-brand focus-within:ring-2 focus-within:ring-brand/20 dark:border-zinc-700 dark:bg-zinc-900">
        <label className="sr-only" htmlFor="cc-composer">
          {strings.composerLabel}
        </label>
        <textarea
          id="cc-composer"
          ref={(el) => {
            innerRef.current = el;
            if (typeof forwardedRef === 'function') forwardedRef(el);
            else if (forwardedRef) forwardedRef.current = el;
          }}
          rows={1}
          value={text}
          maxLength={MAX_MESSAGE_LENGTH}
          placeholder={strings.composerPlaceholder}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          className="cc-scroll max-h-[120px] flex-1 resize-none bg-transparent py-1.5 text-sm text-zinc-900 outline-none placeholder:text-zinc-400 dark:text-zinc-100"
        />
        <button
          type="submit"
          aria-label={strings.send}
          disabled={!canSend}
          className={cn(
            'mb-0.5 flex size-8 shrink-0 cursor-pointer items-center justify-center rounded-full transition',
            'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand',
            canSend ? 'bg-brand text-brand-fg hover:brightness-110' : 'bg-zinc-200 text-zinc-400 dark:bg-zinc-700',
          )}
        >
          <ArrowUp className="size-4" aria-hidden="true" />
        </button>
      </div>
      {text.length >= COUNTER_FROM && (
        <p className="mt-1 text-right text-[11px] text-zinc-400" aria-live="polite">
          {text.length}/{MAX_MESSAGE_LENGTH}
        </p>
      )}
    </form>
  );
});
