import { createSlice } from '@reduxjs/toolkit';
import type { RootState } from '@/app/store';
import type { CartSnapshot, OrderStatus } from '@/shared/api/contracts';
import { gatewayApi } from '@/shared/api/gatewayApi';
import { blockReceived, historyLoaded } from '@/features/chat/chatSlice';

/** Once an order reaches one of these, the backend has taken its items out of the cart. */
const PAID_STATUSES = new Set<OrderStatus>(['PAID', 'FULFILLED', 'FULFILLMENT_FAILED', 'REFUND_PENDING', 'REFUNDED']);

/**
 * The cart shown in the header badge and in the cart view is the most recent
 * cart snapshot the backend emitted. The widget never edits it locally: every
 * change goes through a turn, and the backend replies with a new snapshot.
 * The one exception: when an order is paid the backend empties the cart it
 * came from, so the stale snapshot is dropped (the next reply carries the new one).
 */
interface CartState {
  snapshot: CartSnapshot | null;
}

const initialState: CartState = { snapshot: null };

const cartSlice = createSlice({
  name: 'cart',
  initialState,
  reducers: {},
  extraReducers: (builder) => {
    builder
      .addCase(blockReceived, (state, action) => {
        if (action.payload.block.type === 'cart') state.snapshot = action.payload.block.cart;
      })
      .addCase(historyLoaded, (state, action) => {
        for (const message of action.payload) {
          for (const block of message.blocks) {
            if (block.type === 'cart') state.snapshot = block.cart; // last one wins
          }
        }
      })
      .addMatcher(gatewayApi.endpoints.getOrder.matchFulfilled, (state, action) => {
        if (PAID_STATUSES.has(action.payload.status)) state.snapshot = null;
      });
  },
});

export default cartSlice.reducer;

export const selectCart = (state: RootState) => state.cart.snapshot;
export const selectCartCount = (state: RootState) => state.cart.snapshot?.item_count ?? 0;
