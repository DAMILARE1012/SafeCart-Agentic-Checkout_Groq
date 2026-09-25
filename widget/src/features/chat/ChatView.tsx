import type { Ref } from 'react';
import { Composer } from './Composer';
import { MessageList } from './MessageList';
import { SuggestionChips } from './SuggestionChips';

export function ChatView({ composerRef }: { composerRef?: Ref<HTMLTextAreaElement> }) {
  return (
    <section aria-label="Conversation" className="flex min-h-0 flex-1 flex-col">
      <MessageList />
      <SuggestionChips />
      <Composer ref={composerRef} />
    </section>
  );
}
