import { useCallback } from 'react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import type { UserAction } from '@/shared/api/contracts';
import { sendMessage } from './chatThunks';

/**
 * Shared by the composer and by every interactive block (product cards, cart
 * controls...). UI controls send a readable message plus a structured
 * `action`, so the agent gets exact IDs instead of having to re-parse text.
 */
export function useSendMessage() {
  const dispatch = useAppDispatch();
  const busy = useAppSelector((s) => s.chat.turn.status === 'streaming' || s.session.status !== 'ready');

  const send = useCallback(
    (text: string, action?: UserAction) => {
      void dispatch(sendMessage({ text, ...(action ? { action } : {}) }));
    },
    [dispatch],
  );

  return { send, busy };
}
