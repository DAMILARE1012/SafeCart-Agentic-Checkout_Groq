import { useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { AssistantAvatar } from './AssistantAvatar';
import { useSendMessage } from './useSendMessage';

export function EmptyState() {
  const merchantName = useAppSelector((s) => s.session.merchant?.name ?? s.config.merchantName ?? 'store');
  const { send, busy } = useSendMessage();

  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 px-6 py-10 text-center">
      <AssistantAvatar className="size-12" />
      <div className="space-y-1">
        <p className="text-base font-semibold text-zinc-900 dark:text-zinc-100">{strings.greetingTitle(merchantName)}</p>
        <p className="text-sm text-zinc-500">{strings.greetingBody}</p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        {strings.starterPrompts.map((prompt) => (
          <button
            key={prompt}
            type="button"
            disabled={busy}
            onClick={() => send(prompt)}
            className="cursor-pointer rounded-full border border-zinc-200 px-3 py-1.5 text-xs text-zinc-700 transition hover:border-brand hover:text-brand focus-visible:outline-2 focus-visible:outline-brand disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-300"
          >
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
