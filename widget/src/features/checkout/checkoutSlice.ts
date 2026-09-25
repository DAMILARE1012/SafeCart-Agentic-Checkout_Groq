import { createSlice, type PayloadAction } from '@reduxjs/toolkit';
import type { RootState } from '@/app/store';
import { newId } from '@/shared/lib/id';

export interface CheckoutAttempt {
  status: 'idle' | 'confirming' | 'redirected' | 'failed';
  /** Generated once per quote and reused on every retry: a retry can never create a second payment. */
  idempotencyKey: string;
  orderId: string | null;
  checkoutUrl: string | null;
  popupBlocked: boolean;
  error: string | null;
}

interface CheckoutState {
  byQuoteId: Record<string, CheckoutAttempt>;
}

const initialState: CheckoutState = { byQuoteId: {} };

const checkoutSlice = createSlice({
  name: 'checkout',
  initialState,
  reducers: {
    confirmationRequested: {
      // Randomness belongs in `prepare`, so the reducer stays pure.
      prepare: (quoteId: string) => ({ payload: { quoteId, idempotencyKey: newId() } }),
      reducer(state, action: PayloadAction<{ quoteId: string; idempotencyKey: string }>) {
        const { quoteId, idempotencyKey } = action.payload;
        const existing = state.byQuoteId[quoteId];
        state.byQuoteId[quoteId] = {
          status: 'confirming',
          idempotencyKey: existing?.idempotencyKey ?? idempotencyKey,
          orderId: null,
          checkoutUrl: null,
          popupBlocked: false,
          error: null,
        };
      },
    },
    confirmationSucceeded(
      state,
      action: PayloadAction<{ quoteId: string; orderId: string; checkoutUrl: string; popupBlocked: boolean }>,
    ) {
      const attempt = state.byQuoteId[action.payload.quoteId];
      if (!attempt) return;
      attempt.status = 'redirected';
      attempt.orderId = action.payload.orderId;
      attempt.checkoutUrl = action.payload.checkoutUrl;
      attempt.popupBlocked = action.payload.popupBlocked;
    },
    confirmationFailed(state, action: PayloadAction<{ quoteId: string; error: string }>) {
      const attempt = state.byQuoteId[action.payload.quoteId];
      if (!attempt) return;
      attempt.status = 'failed';
      attempt.error = action.payload.error;
    },
  },
});

export const { confirmationRequested, confirmationSucceeded, confirmationFailed } = checkoutSlice.actions;
export default checkoutSlice.reducer;

export const selectCheckoutAttempt = (state: RootState, quoteId: string): CheckoutAttempt | undefined =>
  state.checkout.byQuoteId[quoteId];
