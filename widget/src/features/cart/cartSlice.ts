import { createSlice } from '@reduxjs/toolkit';
import type { RootState } from '@/app/store';
import type { CartSnapshot } from '@/shared/api/contracts';
import { blockReceived, historyLoaded } from '@/features/chat/chatSlice';

/**
 * The cart shown in the header badge and in the cart view is the most recent
 * cart snapshot the backend emitted. The widget never edits it locally: every
 * change goes through a turn, and the backend replies with a new snapshot.
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
      });
  },
});

export default cartSlice.reducer;

export const selectCart = (state: RootState) => state.cart.snapshot;
export const selectCartCount = (state: RootState) => state.cart.snapshot?.item_count ?? 0;
