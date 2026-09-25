import { describe, expect, it } from 'vitest';
import { resolveConfig } from '@/app/config';
import { makeStore } from '@/app/store';
import { blockReceived } from '@/features/chat/chatSlice';
import type { CartSnapshot, OrderStatus, OrderSummary } from '@/shared/api/contracts';
import { gatewayApi } from '@/shared/api/gatewayApi';
import { selectCartCount } from './cartSlice';

const usd = (amount_minor: number) => ({ amount_minor, currency: 'USD' });
const cart: CartSnapshot = { id: 'cart_1', lines: [], item_count: 1, subtotal: usd(18900), discounts: [] };
const order = (status: OrderStatus): OrderSummary => ({
  id: 'ord_1',
  status,
  total: usd(20459),
  checkout_url: null,
  refund: null,
  updated_at: '2026-09-25T10:05:38Z',
});

function storeWithCart() {
  const store = makeStore(resolveConfig({ publishableKey: 'pk_test', gatewayBaseUrl: 'http://gateway' }));
  store.dispatch(blockReceived({ messageId: 'm1', block: { type: 'cart', cart } }));
  return store;
}

describe('cartSlice', () => {
  it('drops the cart once its order is paid (the backend emptied it)', async () => {
    const store = storeWithCart();
    expect(selectCartCount(store.getState())).toBe(1);

    await store.dispatch(gatewayApi.util.upsertQueryData('getOrder', 'ord_1', order('PAID')));

    expect(selectCartCount(store.getState())).toBe(0);
  });

  it('keeps the cart while payment is still pending', async () => {
    const store = storeWithCart();

    await store.dispatch(gatewayApi.util.upsertQueryData('getOrder', 'ord_1', order('AWAITING_PAYMENT')));

    expect(selectCartCount(store.getState())).toBe(1);
  });
});
