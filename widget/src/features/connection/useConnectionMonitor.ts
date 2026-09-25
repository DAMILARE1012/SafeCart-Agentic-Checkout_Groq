import { useEffect } from 'react';
import { useAppDispatch, useAppSelector } from '@/app/hooks';
import type { ConversationStreamEvent } from '@/shared/api/contracts';
import { gatewayApi } from '@/shared/api/gatewayApi';
import { newId } from '@/shared/lib/id';
import { HttpError, streamSse } from '@/shared/lib/sse';
import { noticeAdded } from '@/features/chat/chatSlice';
import { renewSession } from '@/features/session/sessionSlice';
import { unreadIncremented } from '@/features/ui/uiSlice';
import { eventsStatusChanged, networkChanged } from './connectionSlice';

const MIN_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;

const sleep = (ms: number, signal: AbortSignal) =>
  new Promise<void>((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      clearTimeout(timer);
      resolve();
    });
  });

/**
 * Tracks browser connectivity and holds the server-push stream open
 * (order updates after payment, compensation notices), reconnecting with
 * exponential backoff and jitter.
 */
export function useConnectionMonitor(): void {
  const dispatch = useAppDispatch();
  const baseUrl = useAppSelector((s) => s.config.gatewayBaseUrl);
  const conversationId = useAppSelector((s) => s.session.conversationId);
  const token = useAppSelector((s) => s.session.token);
  const network = useAppSelector((s) => s.connection.network);

  useEffect(() => {
    const online = () => dispatch(networkChanged('online'));
    const offline = () => dispatch(networkChanged('offline'));
    window.addEventListener('online', online);
    window.addEventListener('offline', offline);
    return () => {
      window.removeEventListener('online', online);
      window.removeEventListener('offline', offline);
    };
  }, [dispatch]);

  useEffect(() => {
    if (!conversationId || !token || network === 'offline') return;
    const controller = new AbortController();

    (async () => {
      let backoff = MIN_BACKOFF_MS;
      while (!controller.signal.aborted) {
        try {
          await streamSse({
            url: `${baseUrl}/v1/conversations/${conversationId}/events`,
            init: { method: 'GET', headers: { Authorization: `Bearer ${token}` } },
            signal: controller.signal,
            onOpen: () => {
              dispatch(eventsStatusChanged('connected'));
              backoff = MIN_BACKOFF_MS;
            },
            onEvent: ({ event, data }) => {
              const e = { event, data } as ConversationStreamEvent;
              if (e.event === 'order.updated') {
                // Push updates straight into the RTK Query cache that OrderTracker reads.
                dispatch(gatewayApi.util.upsertQueryData('getOrder', e.data.id, e.data));
              } else if (e.event === 'notice') {
                dispatch(noticeAdded({ id: newId(), text: e.data.text }));
                dispatch(unreadIncremented());
              }
            },
          });
        } catch (error) {
          if (controller.signal.aborted) return;
          if (error instanceof HttpError && error.status === 401) {
            // A new token re-runs this effect with fresh credentials.
            void dispatch(renewSession());
            return;
          }
        }
        if (controller.signal.aborted) return;
        dispatch(eventsStatusChanged('reconnecting'));
        await sleep(backoff + Math.random() * 500, controller.signal);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
      }
    })();

    return () => {
      controller.abort();
      dispatch(eventsStatusChanged('idle'));
    };
  }, [baseUrl, conversationId, token, network, dispatch]);
}
