import { createEntityAdapter, createSlice, type PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '@/app/store';
import type { ApiErrorBody, HistoryMessage, TurnRequest, UiBlock } from '@/shared/api/contracts';

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  blocks: UiBlock[];
  suggestions: string[];
  status: 'sending' | 'streaming' | 'complete' | 'failed';
  createdAt: string;
  /** User messages keep their original request so a retry replays it with the same idempotency key. */
  request?: TurnRequest;
  error?: ApiErrorBody;
}

const adapter = createEntityAdapter<ChatMessage>();

interface TurnState {
  status: 'idle' | 'streaming';
  userMessageId: string | null;
  assistantMessageId: string | null;
}

const initialState = adapter.getInitialState({
  turn: { status: 'idle', userMessageId: null, assistantMessageId: null } as TurnState,
  historyLoaded: false,
});

const chatSlice = createSlice({
  name: 'chat',
  initialState,
  reducers: {
    userMessageAdded(state, action: PayloadAction<TurnRequest>) {
      const request = action.payload;
      adapter.addOne(state, {
        id: request.client_message_id,
        role: 'user',
        text: request.text,
        blocks: [],
        suggestions: [],
        status: 'sending',
        createdAt: new Date().toISOString(),
        request,
      });
      state.turn = { status: 'streaming', userMessageId: request.client_message_id, assistantMessageId: null };
    },

    retryStarted(state, action: PayloadAction<string>) {
      adapter.updateOne(state, { id: action.payload, changes: { status: 'sending', error: undefined } });
      state.turn = { status: 'streaming', userMessageId: action.payload, assistantMessageId: null };
    },

    turnStarted(state, action: PayloadAction<{ userMessageId: string; messageId: string }>) {
      const { userMessageId, messageId } = action.payload;
      adapter.updateOne(state, { id: userMessageId, changes: { status: 'complete' } });
      adapter.upsertOne(state, {
        id: messageId,
        role: 'assistant',
        text: '',
        blocks: [],
        suggestions: [],
        status: 'streaming',
        createdAt: new Date().toISOString(),
      });
      state.turn.assistantMessageId = messageId;
    },

    textDelta(state, action: PayloadAction<{ messageId: string; delta: string }>) {
      const message = state.entities[action.payload.messageId];
      if (message) message.text += action.payload.delta;
    },

    blockReceived(state, action: PayloadAction<{ messageId: string; block: UiBlock }>) {
      state.entities[action.payload.messageId]?.blocks.push(action.payload.block);
    },

    suggestionsReceived(state, action: PayloadAction<{ messageId: string; suggestions: string[] }>) {
      const message = state.entities[action.payload.messageId];
      if (message) message.suggestions = action.payload.suggestions;
    },

    turnCompleted(state, action: PayloadAction<{ messageId: string }>) {
      adapter.updateOne(state, { id: action.payload.messageId, changes: { status: 'complete' } });
      state.turn = { status: 'idle', userMessageId: null, assistantMessageId: null };
    },

    turnFailed(state, action: PayloadAction<{ userMessageId: string; error: ApiErrorBody }>) {
      const { userMessageId, error } = action.payload;
      const assistantId = state.turn.assistantMessageId;
      const assistant = assistantId ? state.entities[assistantId] : undefined;
      if (assistant && !assistant.text && assistant.blocks.length === 0) {
        adapter.removeOne(state, assistant.id);
      } else if (assistant) {
        assistant.status = 'complete';
      }
      adapter.updateOne(state, { id: userMessageId, changes: { status: 'failed', error } });
      state.turn = { status: 'idle', userMessageId: null, assistantMessageId: null };
    },

    historyLoaded(state, action: PayloadAction<HistoryMessage[]>) {
      adapter.setAll(
        state,
        action.payload.map((m) => ({
          id: m.id,
          role: m.role,
          text: m.text,
          blocks: m.blocks,
          suggestions: m.suggestions ?? [],
          status: 'complete' as const,
          createdAt: m.created_at,
        })),
      );
      state.historyLoaded = true;
    },

    noticeAdded(state, action: PayloadAction<{ id: string; text: string }>) {
      adapter.addOne(state, {
        id: action.payload.id,
        role: 'system',
        text: action.payload.text,
        blocks: [],
        suggestions: [],
        status: 'complete',
        createdAt: new Date().toISOString(),
      });
    },
  },
});

export const {
  userMessageAdded,
  retryStarted,
  turnStarted,
  textDelta,
  blockReceived,
  suggestionsReceived,
  turnCompleted,
  turnFailed,
  historyLoaded,
  noticeAdded,
} = chatSlice.actions;

export default chatSlice.reducer;

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------
const selectors = adapter.getSelectors((state: RootState) => state.chat);
export const selectMessageIds = selectors.selectIds;
export const selectMessageById = selectors.selectById;
export const selectAllMessages = selectors.selectAll;
export const selectTurn = (state: RootState) => state.chat.turn;
export const selectIsStreaming = (state: RootState) => state.chat.turn.status === 'streaming';

const NO_SUGGESTIONS: string[] = [];

/**
 * Suggestions are only shown for the latest assistant message, and only when idle.
 * Returns stable references (the stored array or a shared empty one) so subscribers
 * don't re-render on unrelated store updates.
 */
export const selectActiveSuggestions = (state: RootState): string[] => {
  if (state.chat.turn.status !== 'idle') return NO_SUGGESTIONS;
  const ids = state.chat.ids;
  const lastId = ids[ids.length - 1];
  const last = lastId ? state.chat.entities[lastId] : undefined;
  return last?.role === 'assistant' ? last.suggestions : NO_SUGGESTIONS;
};
