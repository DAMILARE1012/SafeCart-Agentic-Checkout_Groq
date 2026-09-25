import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { Provider } from 'react-redux';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { handlers } from '@/mocks/handlers';
import { resolveConfig } from './config';
import { makeStore } from './store';
import { Widget } from './Widget';

const server = setupServer(...handlers);
const T = { timeout: 5000 };

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' });
  // Browser APIs missing from jsdom
  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
  Element.prototype.scrollTo ??= () => {};
});
afterEach(() => {
  cleanup();
  window.localStorage.clear();
});
afterAll(() => server.close());

function renderWidget() {
  const store = makeStore(resolveConfig({ publishableKey: 'pk_test', gatewayBaseUrl: 'http://gateway.test', locale: 'en-US' }));
  render(
    <Provider store={store}>
      <Widget />
    </Provider>,
  );
  return store;
}

describe('Widget: browse → cart → explicit confirmation → order tracking', () => {
  it('only starts payment after the user clicks Confirm & pay, with a stable idempotency key', async () => {
    const paymentWindow = { closed: false, opener: {}, location: { href: '' }, document: { title: '', body: { style: {}, textContent: '' } }, close: vi.fn() };
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(paymentWindow as unknown as Window);
    const store = renderWidget();

    // Lazy bootstrap: nothing happens until the panel opens
    expect(store.getState().session.status).toBe('idle');
    fireEvent.click(screen.getByRole('button', { name: /open shopping assistant/i }));
    fireEvent.click(await screen.findByRole('button', { name: 'Show me running shoes' }, T));

    // Streamed reply with a product carousel
    expect(await screen.findByText('Trail Runner GTX', {}, T)).toBeTruthy();
    const addButtons = await screen.findAllByRole('button', { name: /add to cart/i });
    await waitFor(() => expect(addButtons[0]?.hasAttribute('disabled')).toBe(false), T);
    fireEvent.click(addButtons[0]!);

    // Cart block comes from the backend snapshot
    expect(await screen.findByText('Added Trail Runner GTX to your cart.', {}, T)).toBeTruthy();
    await waitFor(() => expect(store.getState().cart.snapshot?.item_count).toBe(1), T); // block follows the text

    // Both the cart card and a suggestion chip say "Checkout"; use the cart card's (first in the DOM).
    const [checkoutButton] = await screen.findAllByRole('button', { name: 'Checkout' });
    if (!checkoutButton) throw new Error('checkout button missing');
    await waitFor(() => expect(checkoutButton.hasAttribute('disabled')).toBe(false), T);
    fireEvent.click(checkoutButton);

    // Quote: $129.00 + $10.64 tax (8.25%), free shipping → $139.64, all computed by the (mock) backend
    const confirm = await screen.findByRole('button', { name: /confirm & pay \$139\.64/i }, T);
    expect(openSpy).not.toHaveBeenCalled(); // no payment without an explicit click

    fireEvent.click(confirm);
    expect(openSpy).toHaveBeenCalledTimes(1); // placeholder tab opened synchronously in the gesture

    // Order tracking begins; the placeholder tab is sent to the checkout URL
    expect(await screen.findByText('Awaiting payment', {}, T)).toBeTruthy();
    expect(paymentWindow.location.href).toContain('/mock-checkout.html?order=ord_');
    expect(paymentWindow.opener).toBeNull();

    const attempt = Object.values(store.getState().checkout.byQuoteId)[0];
    expect(attempt?.status).toBe('redirected');
    expect(attempt?.idempotencyKey).toMatch(/[0-9a-f-]{16,}/);
  }, 20_000);

  it('shows a retryable error and succeeds on retry', async () => {
    renderWidget();
    fireEvent.click(screen.getByRole('button', { name: /open shopping assistant/i }));
    const composer = await screen.findByRole('textbox', { name: /message/i }, T);
    fireEvent.change(composer, { target: { value: 'simulate error' } });
    fireEvent.submit(composer.closest('form')!);

    const retry = await screen.findByRole('button', { name: /retry/i }, T);
    fireEvent.click(retry);
    expect(await screen.findByText(/I can help you find products/, {}, T)).toBeTruthy();
  }, 20_000);
});
