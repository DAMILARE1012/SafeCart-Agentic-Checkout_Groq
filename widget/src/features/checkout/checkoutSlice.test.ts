import { describe, expect, it } from 'vitest';
import reducer, { confirmationFailed, confirmationRequested, confirmationSucceeded } from './checkoutSlice';

describe('checkoutSlice', () => {
  it('keeps the same idempotency key across retries of the same quote', () => {
    let state = reducer(undefined, confirmationRequested('q_1'));
    const firstKey = state.byQuoteId.q_1?.idempotencyKey;
    expect(firstKey).toBeTruthy();

    state = reducer(state, confirmationFailed({ quoteId: 'q_1', error: 'network' }));
    state = reducer(state, confirmationRequested('q_1'));

    expect(state.byQuoteId.q_1?.idempotencyKey).toBe(firstKey);
    expect(state.byQuoteId.q_1?.status).toBe('confirming');
  });

  it('uses different keys for different quotes', () => {
    let state = reducer(undefined, confirmationRequested('q_1'));
    state = reducer(state, confirmationRequested('q_2'));
    expect(state.byQuoteId.q_1?.idempotencyKey).not.toBe(state.byQuoteId.q_2?.idempotencyKey);
  });

  it('records the order and whether the popup was blocked', () => {
    let state = reducer(undefined, confirmationRequested('q_1'));
    state = reducer(
      state,
      confirmationSucceeded({ quoteId: 'q_1', orderId: 'ord_1', checkoutUrl: 'https://pay', popupBlocked: true }),
    );
    expect(state.byQuoteId.q_1).toMatchObject({ status: 'redirected', orderId: 'ord_1', popupBlocked: true });
  });
});
