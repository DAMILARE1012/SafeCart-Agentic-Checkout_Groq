import { createSlice, type PayloadAction } from '@reduxjs/toolkit';

export type PanelView = 'chat' | 'cart';

interface UiState {
  isOpen: boolean;
  view: PanelView;
  unread: number;
}

const initialState: UiState = { isOpen: false, view: 'chat', unread: 0 };

const uiSlice = createSlice({
  name: 'ui',
  initialState,
  reducers: {
    panelOpened(state) {
      state.isOpen = true;
      state.unread = 0;
    },
    panelClosed(state) {
      state.isOpen = false;
    },
    viewChanged(state, action: PayloadAction<PanelView>) {
      state.view = action.payload;
    },
    unreadIncremented(state) {
      if (!state.isOpen) state.unread += 1;
    },
  },
});

export const { panelOpened, panelClosed, viewChanged, unreadIncremented } = uiSlice.actions;
export default uiSlice.reducer;
