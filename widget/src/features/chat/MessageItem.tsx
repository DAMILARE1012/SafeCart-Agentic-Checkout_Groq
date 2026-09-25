import { RotateCw } from 'lucide-react';
import { memo } from 'react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { cn } from '@/shared/lib/cn';
import { BlockRenderer } from '@/features/blocks/BlockRenderer';
import { AssistantAvatar } from './AssistantAvatar';
import { selectIsStreaming, selectMessageById } from './chatSlice';
import { retryMessage } from './chatThunks';
import { TypingIndicator } from './TypingIndicator';

/**
 * One message row. Memoised by id, so a streaming delta only re-renders the
 * message it belongs to. Text is rendered as plain text (never as HTML), so
 * model output can't inject markup.
 */
export const MessageItem = memo(function MessageItem({ id }: { id: string }) {
  const dispatch = useAppDispatch();
  const message = useAppSelector((s) => selectMessageById(s, id));
  const streaming = useAppSelector(selectIsStreaming);
  if (!message) return null;

  if (message.role === 'system') {
    return (
      <li className="flex justify-center">
        <p className="rounded-full bg-zinc-100 px-3 py-1 text-center text-xs text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">
          {message.text}
        </p>
      </li>
    );
  }

  if (message.role === 'user') {
    const failed = message.status === 'failed';
    return (
      <li className="flex flex-col items-end gap-1">
        <p
          className={cn(
            'max-w-[85%] rounded-2xl rounded-br-md px-3.5 py-2 break-words whitespace-pre-wrap',
            failed ? 'border border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-950/40 dark:text-rose-200' : 'bg-brand text-brand-fg',
            message.status === 'sending' && 'opacity-70',
          )}
        >
          {message.text}
        </p>
        {failed && (
          <div role="alert" className="flex items-center gap-2 text-xs text-rose-600 dark:text-rose-400">
            <span>{message.error?.message ?? strings.turnError}</span>
            {message.error?.retryable !== false && (
              <button
                type="button"
                disabled={streaming}
                onClick={() => void dispatch(retryMessage(message.id))}
                className="inline-flex cursor-pointer items-center gap-1 font-medium underline-offset-2 hover:underline disabled:opacity-50"
              >
                <RotateCw className="size-3" aria-hidden="true" />
                {strings.retry}
              </button>
            )}
          </div>
        )}
      </li>
    );
  }

  const isStreaming = message.status === 'streaming';
  const showTyping = isStreaming && !message.text && message.blocks.length === 0;

  return (
    <li className="flex gap-2">
      <AssistantAvatar className="mt-0.5" />
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        {(message.text || showTyping) && (
          <div className="w-fit max-w-[92%] rounded-2xl rounded-tl-md bg-zinc-100 px-3.5 py-2 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100">
            {showTyping ? (
              <TypingIndicator />
            ) : (
              <p className="break-words whitespace-pre-wrap">
                {message.text}
                {isStreaming && (
                  <span aria-hidden="true" className="ml-0.5 inline-block h-4 w-0.5 translate-y-0.5 animate-pulse bg-current" />
                )}
              </p>
            )}
          </div>
        )}
        {message.blocks.map((block, i) => (
          <BlockRenderer key={i} block={block} />
        ))}
      </div>
    </li>
  );
});
