import { combineReducers, configureStore } from '@reduxjs/toolkit';
import { gatewayApi } from '@/shared/api/gatewayApi';
import cartReducer from '@/features/cart/cartSlice';
import chatReducer from '@/features/chat/chatSlice';
import checkoutReducer from '@/features/checkout/checkoutSlice';
import connectionReducer from '@/features/connection/connectionSlice';
import sessionReducer from '@/features/session/sessionSlice';
import uiReducer from '@/features/ui/uiSlice';
import type { WidgetConfig } from './config';
import configReducer from './configSlice';

const rootReducer = combineReducers({
  config: configReducer,
  session: sessionReducer,
  chat: chatReducer,
  cart: cartReducer,
  checkout: checkoutReducer,
  connection: connectionReducer,
  ui: uiReducer,
  [gatewayApi.reducerPath]: gatewayApi.reducer,
});

export type RootState = ReturnType<typeof rootReducer>;

/** One store per widget instance: no globals shared with the host page. */
export function makeStore(config: WidgetConfig) {
  return configureStore({
    reducer: rootReducer,
    preloadedState: { config },
    middleware: (getDefault) => getDefault().concat(gatewayApi.middleware),
    devTools: import.meta.env.DEV ? { name: 'CommerceChat widget' } : false,
  });
}

export type AppStore = ReturnType<typeof makeStore>;
export type AppDispatch = AppStore['dispatch'];
