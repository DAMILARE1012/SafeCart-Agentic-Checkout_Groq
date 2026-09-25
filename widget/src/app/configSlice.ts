import { createSlice } from '@reduxjs/toolkit';
import type { WidgetConfig } from './config';

/** Static, per-instance configuration. Provided via preloadedState, so there are no reducers. */
const configSlice = createSlice({
  name: 'config',
  initialState: {} as WidgetConfig,
  reducers: {},
});

export default configSlice.reducer;
