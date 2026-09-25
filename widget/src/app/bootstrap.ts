import { gatewayApi } from '@/shared/api/gatewayApi';
import { historyLoaded } from '@/features/chat/chatSlice';
import { bootstrapSession } from '@/features/session/sessionSlice';
import { createAppAsyncThunk } from './hooks';

/**
 * Starts (or resumes) the widget session. Called lazily on first open, or
 * immediately if a previous session exists (so order tracking resumes after a reload).
 */
export const startWidget = createAppAsyncThunk('app/start', async (_: void, { dispatch, getState }) => {
  await dispatch(bootstrapSession());
  const { session, chat } = getState();
  if (session.status !== 'ready' || !session.resumed || chat.historyLoaded || !session.conversationId) return;

  const history = dispatch(gatewayApi.endpoints.getHistory.initiate(session.conversationId));
  try {
    dispatch(historyLoaded(await history.unwrap()));
  } catch {
    /* history is a convenience; the conversation continues without it */
  } finally {
    history.unsubscribe();
  }
});
