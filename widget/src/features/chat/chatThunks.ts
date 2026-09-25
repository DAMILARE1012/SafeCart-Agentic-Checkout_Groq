import { createAppAsyncThunk } from '@/app/hooks';
import type { AppDispatch, RootState } from '@/app/store';
import type { ApiErrorBody, TurnRequest, TurnStreamEvent, UserAction } from '@/shared/api/contracts';
import { newId } from '@/shared/lib/id';
import { HttpError, streamSse, type SseEvent } from '@/shared/lib/sse';
import { renewSession } from '@/features/session/sessionSlice';
import { unreadIncremented } from '@/features/ui/uiSlice';
import {
  blockReceived,
  retryStarted,
  suggestionsReceived,
  textDelta,
  turnCompleted,
  turnFailed,
  turnStarted,
  userMessageAdded,
} from './chatSlice';

const MAX_MESSAGE_LENGTH = 1000;

const GENERIC_ERROR: ApiErrorBody = {
  code: 'turn_failed',
  message: 'Something went wrong. Your cart is saved.',
  retryable: true,
};

function toApiError(error: unknown): ApiErrorBody {
  if (error instanceof HttpError && error.body && typeof error.body === 'object' && 'code' in error.body) {
    return error.body as ApiErrorBody;
  }
  return GENERIC_ERROR;
}

/**
 * Streams one turn. Returns true only if the server sent `turn.completed`:
 * a stream that closes early counts as a failure.
 */
async function streamTurn(
  request: TurnRequest,
  ctx: { dispatch: AppDispatch; baseUrl: string; conversationId: string; token: string; signal: AbortSignal },
): Promise<{ completed: boolean; error?: ApiErrorBody }> {
  let completed = false;
  let streamError: ApiErrorBody | undefined;

  const onEvent = ({ event, data }: SseEvent) => {
    const e = { event, data } as TurnStreamEvent;
    switch (e.event) {
      case 'turn.started':
        ctx.dispatch(turnStarted({ userMessageId: request.client_message_id, messageId: e.data.message_id }));
        break;
      case 'text.delta':
        ctx.dispatch(textDelta({ messageId: e.data.message_id, delta: e.data.delta }));
        break;
      case 'block':
        ctx.dispatch(blockReceived({ messageId: e.data.message_id, block: e.data.block }));
        break;
      case 'suggestions':
        ctx.dispatch(suggestionsReceived({ messageId: e.data.message_id, suggestions: e.data.suggestions }));
        break;
      case 'turn.completed':
        completed = true;
        ctx.dispatch(turnCompleted({ messageId: e.data.message_id }));
        break;
      case 'turn.error':
        streamError = { code: e.data.code, message: e.data.message, retryable: e.data.retryable };
        break;
      default:
        break; // unknown events are ignored (forward compatible)
    }
  };

  await streamSse({
    url: `${ctx.baseUrl}/v1/conversations/${ctx.conversationId}/turns`,
    init: {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${ctx.token}`,
        // Same key on retry → the gateway replays instead of running the turn twice.
        'Idempotency-Key': request.client_message_id,
      },
      body: JSON.stringify(request),
    },
    signal: ctx.signal,
    onEvent,
  });

  return { completed, error: streamError };
}

const runTurn = createAppAsyncThunk(
  'chat/runTurn',
  async (request: TurnRequest, { dispatch, getState, signal }) => {
    const attempt = () => {
      const { config, session } = getState();
      return streamTurn(request, {
        dispatch,
        signal,
        baseUrl: config.gatewayBaseUrl,
        conversationId: session.conversationId ?? '',
        token: session.token ?? '',
      });
    };

    try {
      let result: Awaited<ReturnType<typeof attempt>>;
      try {
        result = await attempt();
      } catch (error) {
        if (!(error instanceof HttpError && error.status === 401)) throw error;
        await dispatch(renewSession()).unwrap();
        result = await attempt();
      }
      if (!result.completed) {
        dispatch(turnFailed({ userMessageId: request.client_message_id, error: result.error ?? GENERIC_ERROR }));
        return;
      }
      if (!getState().ui.isOpen) dispatch(unreadIncremented());
    } catch (error) {
      dispatch(turnFailed({ userMessageId: request.client_message_id, error: toApiError(error) }));
    }
  },
);

/** One turn at a time, and only with a live session (mirrors the server-side per-conversation lock). */
const canStartTurn = (state: RootState) =>
  state.chat.turn.status === 'idle' && state.session.status === 'ready';

export const sendMessage = createAppAsyncThunk(
  'chat/sendMessage',
  async ({ text, action }: { text: string; action?: UserAction }, { dispatch }) => {
    const request: TurnRequest = {
      client_message_id: newId(),
      text: text.trim().slice(0, MAX_MESSAGE_LENGTH),
      ...(action ? { action } : {}),
    };
    dispatch(userMessageAdded(request));
    await dispatch(runTurn(request));
  },
  {
    condition: ({ text }, { getState }) => text.trim().length > 0 && canStartTurn(getState()),
  },
);

export const retryMessage = createAppAsyncThunk(
  'chat/retryMessage',
  async (messageId: string, { dispatch, getState }) => {
    const request = getState().chat.entities[messageId]?.request;
    if (!request) return;
    dispatch(retryStarted(messageId));
    await dispatch(runTurn(request));
  },
  { condition: (_, { getState }) => canStartTurn(getState()) },
);

export { MAX_MESSAGE_LENGTH };
