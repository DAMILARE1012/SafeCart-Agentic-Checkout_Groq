/**
 * Browsers only allow `window.open` synchronously inside a user gesture. We
 * open a placeholder tab on click, then point it at Stripe once the backend
 * returns the Checkout URL. Windows aren't serializable, so they're kept here
 * rather than in Redux.
 */
const windows = new Map<string, Window>();

export function openPaymentPlaceholder(quoteId: string): void {
  const win = window.open('', '_blank');
  if (!win) return; // popup blocked; the UI falls back to a link
  try {
    win.opener = null; // the Stripe page must not be able to script the storefront
    win.document.title = 'Redirecting to secure checkout…';
    win.document.body.style.cssText = 'font-family:system-ui,sans-serif;display:grid;place-items:center;height:100vh;margin:0;color:#3f3f46';
    win.document.body.textContent = 'Redirecting to secure checkout…';
  } catch {
    /* cross-origin restrictions: harmless */
  }
  windows.set(quoteId, win);
}

/** Navigates the placeholder. Returns false if it was blocked or closed by the user. */
export function navigatePaymentWindow(quoteId: string, url: string): boolean {
  const win = windows.get(quoteId);
  windows.delete(quoteId);
  if (!win || win.closed) return false;
  win.location.href = url;
  return true;
}

export function closePaymentWindow(quoteId: string): void {
  windows.get(quoteId)?.close();
  windows.delete(quoteId);
}
