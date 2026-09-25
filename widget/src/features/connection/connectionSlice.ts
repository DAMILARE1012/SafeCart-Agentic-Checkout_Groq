import { createSlice, type PayloadAction } from '@reduxjs/toolkit';

interface ConnectionState {
  network: 'online' | 'offline';
  /** Server-push channel (order updates, notices). */
  events: 'idle' | 'connected' | 'reconnecting';
}

const initialState: ConnectionState = {
  network: typeof navigator !== 'undefined' && navigator.onLine === false ? 'offline' : 'online',
  events: 'idle',
};

const connectionSlice = createSlice({
  name: 'connection',
  initialState,
  reducers: {
    networkChanged(state, action: PayloadAction<'online' | 'offline'>) {
      state.network = action.payload;
    },
    eventsStatusChanged(state, action: PayloadAction<ConnectionState['events']>) {
      state.events = action.payload;
    },
  },
});

export const { networkChanged, eventsStatusChanged } = connectionSlice.actions;
export default connectionSlice.reducer;
