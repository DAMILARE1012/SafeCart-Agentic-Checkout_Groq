import { createSlice } from '@reduxjs/toolkit';
import { createAppAsyncThunk } from '@/app/hooks';
import type { RootState } from '@/app/store';
import type { MerchantInfo, SessionResponse } from '@/shared/api/contracts';
import { HttpError } from '@/shared/lib/sse';
import { safeStorage } from '@/shared/lib/storage';

interface StoredSession {
  token: string;
  expiresAt: string;
  conversationId: string;
  merchant: MerchantInfo;
}

export interface SessionState {
  status: 'idle' | 'loading' | 'ready' | 'error';
  token: string | null;
  expiresAt: string | null;
  conversationId: string | null;
  merchant: MerchantInfo | null;
  /** True when an existing conversation was resumed (history should be loaded). */
  resumed: boolean;
  error: string | null;
}

const initialState: SessionState = {
  status: 'idle',
  token: null,
  expiresAt: null,
  conversationId: null,
  merchant: null,
  resumed: false,
  error: null,
};

const RENEW_MARGIN_MS = 60_000;
const storageKey = (state: RootState) => `cc:session:${state.config.publishableKey}`;

export function readStoredSession(state: RootState): StoredSession | null {
  return safeStorage.read<StoredSession>(storageKey(state));
}

async function requestSession(state: RootState, resumeConversationId?: string): Promise<StoredSession> {
  const { config } = state;
  const response = await fetch(`${config.gatewayBaseUrl}/v1/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      publishable_key: config.publishableKey,
      locale: config.locale,
      ...(config.currency ? { currency: config.currency } : {}),
      ...(resumeConversationId ? { resume_conversation_id: resumeConversationId } : {}),
    }),
  });
  if (!response.ok) throw new HttpError(response.status, await response.json().catch(() => null));
  const body = (await response.json()) as SessionResponse;
  const session: StoredSession = {
    token: body.session_token,
    expiresAt: body.expires_at,
    conversationId: body.conversation_id,
    merchant: body.merchant,
  };
  safeStorage.write(storageKey(state), session);
  return session;
}

/** Restores a still-valid stored session, or creates one (resuming the stored conversation if possible). */
export const bootstrapSession = createAppAsyncThunk(
  'session/bootstrap',
  async (_: void, { getState }) => {
    const state = getState();
    const stored = readStoredSession(state);
    if (stored && Date.parse(stored.expiresAt) - Date.now() > RENEW_MARGIN_MS) {
      return { session: stored, resumed: true };
    }
    const session = await requestSession(state, stored?.conversationId);
    return { session, resumed: session.conversationId === stored?.conversationId };
  },
  {
    condition: (_, { getState }) => {
      const { status } = getState().session;
      return status === 'idle' || status === 'error';
    },
  },
);

// Concurrent 401s (e.g. a turn and an order poll) share one renewal.
let inflightRenewal: Promise<StoredSession> | null = null;

/** Forces a fresh token for the current conversation (used after a 401). */
export const renewSession = createAppAsyncThunk('session/renew', async (_: void, { getState }) => {
  const state = getState();
  inflightRenewal ??= requestSession(state, state.session.conversationId ?? undefined).finally(() => {
    inflightRenewal = null;
  });
  return { session: await inflightRenewal, resumed: true };
});

const sessionSlice = createSlice({
  name: 'session',
  initialState,
  reducers: {},
  extraReducers: (builder) => {
    builder
      .addCase(bootstrapSession.pending, (state) => {
        state.status = 'loading';
        state.error = null;
      })
      .addCase(bootstrapSession.rejected, (state, action) => {
        state.status = 'error';
        state.error = action.error.message ?? 'session_failed';
      })
      .addMatcher(
        (action) => bootstrapSession.fulfilled.match(action) || renewSession.fulfilled.match(action),
        (state, action: ReturnType<typeof bootstrapSession.fulfilled>) => {
          const { session, resumed } = action.payload;
          state.status = 'ready';
          state.token = session.token;
          state.expiresAt = session.expiresAt;
          state.conversationId = session.conversationId;
          state.merchant = session.merchant;
          state.resumed = state.resumed || resumed;
          state.error = null;
        },
      );
  },
});

export default sessionSlice.reducer;
