import { useAppSelector } from '@/app/hooks';
import { selectActiveSuggestions } from './chatSlice';
import { useSendMessage } from './useSendMessage';

/** Quick replies proposed by the agent for its latest message. */
export function SuggestionChips() {
  const suggestions = useAppSelector(selectActiveSuggestions);
  const { send, busy } = useSendMessage();
  if (suggestions.length === 0) return null;

  return (
    <div className="cc-carousel flex gap-2 overflow-x-auto px-4 pb-2" role="group" aria-label="Suggested replies">
      {suggestions.map((s) => (
        <button
          key={s}
          type="button"
          disabled={busy}
          onClick={() => send(s)}
          className="shrink-0 cursor-pointer rounded-full border border-brand/40 bg-brand/5 px-3 py-1.5 text-xs font-medium text-brand transition hover:bg-brand/10 focus-visible:outline-2 focus-visible:outline-brand disabled:opacity-50"
        >
          {s}
        </button>
      ))}
    </div>
  );
}
