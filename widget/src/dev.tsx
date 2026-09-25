/**
 * Dev playground entry (index.html). Not part of the production bundle.
 * With VITE_USE_MOCKS=true the gateway API is served by Mock Service Worker,
 * so the UI can be built and demoed without running the backend.
 */
async function main() {
  const useMocks = import.meta.env.VITE_USE_MOCKS !== 'false';
  if (useMocks) {
    const { worker } = await import('./mocks/browser');
    await worker.start({ onUnhandledRequest: 'bypass', quiet: true });
  }

  const widget = await import('./embed');
  // Same imperative API the production IIFE exposes as window.CommerceChat
  (window as unknown as { CommerceChat: typeof widget }).CommerceChat = widget;

  const params = new URLSearchParams(window.location.search);
  widget.init({
    publishableKey: import.meta.env.VITE_WIDGET_PUBLISHABLE_KEY ?? 'pk_widget_demo_123',
    gatewayBaseUrl: import.meta.env.VITE_GATEWAY_BASE_URL ?? 'http://localhost',
    merchantName: 'Northwind Outfitters',
    brandColor: params.get('brand') ? `#${params.get('brand')}` : '#0f766e',
    theme: (params.get('theme') as 'light' | 'dark' | 'auto' | null) ?? 'auto',
    position: params.get('position') === 'left' ? 'bottom-left' : 'bottom-right',
    openOnLoad: params.get('open') === '1',
  });
}

void main();
