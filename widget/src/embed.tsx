/**
 * Public entry point: the single `commerce-chat.js` file merchants embed.
 *
 *   <script src="https://cdn.example.com/commerce-chat.js"
 *           data-publishable-key="pk_live_..." data-gateway-url="https://api.example.com"
 *           data-brand-color="#0f766e" async></script>
 *
 * or programmatically: window.CommerceChat.init({ publishableKey, gatewayBaseUrl, ... }).
 *
 * The widget renders inside a Shadow DOM, so storefront CSS can't break it and
 * the widget's CSS can't leak into the storefront.
 */
import { StrictMode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { Provider } from 'react-redux';
import { optionsFromDataset, resolveConfig, type WidgetOptions } from '@/app/config';
import { makeStore, type AppStore } from '@/app/store';
import { Widget } from '@/app/Widget';
import { panelClosed, panelOpened } from '@/features/ui/uiSlice';
import css from '@/styles/widget.css?inline';

const TAG = 'commerce-chat-widget';
const PROPERTY_STYLE_ID = 'cc-widget-properties';

/**
 * Tailwind v4 registers custom properties with `@property`, which browsers
 * ignore inside a shadow root. Registering them once on the document restores
 * shadows, rings and transforms. They're inert registrations with no visual effect on the host.
 */
function hoistPropertyRules(): void {
  if (document.getElementById(PROPERTY_STYLE_ID)) return;
  const rules = css.match(/@property\s+[^{]+\{[^}]*\}/g);
  if (!rules) return;
  const style = document.createElement('style');
  style.id = PROPERTY_STYLE_ID;
  style.textContent = rules.join('\n');
  document.head.appendChild(style);
}

class CommerceChatElement extends HTMLElement {
  options: Partial<WidgetOptions> = {};
  private root: Root | null = null;
  private store: AppStore | null = null;

  connectedCallback(): void {
    if (this.root) return;
    const shadow = this.shadowRoot ?? this.attachShadow({ mode: 'open' });
    shadow.replaceChildren();

    const config = resolveConfig({ ...optionsFromDataset(this.dataset), ...this.options } as WidgetOptions);
    this.store = makeStore(config);

    hoistPropertyRules();
    const style = document.createElement('style');
    style.textContent = css;
    const mount = document.createElement('div');
    shadow.append(style, mount);

    this.root = createRoot(mount);
    this.root.render(
      <StrictMode>
        <Provider store={this.store}>
          <Widget />
        </Provider>
      </StrictMode>,
    );
  }

  disconnectedCallback(): void {
    this.root?.unmount();
    this.root = null;
    this.store = null;
  }

  open(): void {
    this.store?.dispatch(panelOpened());
  }

  close(): void {
    this.store?.dispatch(panelClosed());
  }
}

if (!customElements.get(TAG)) customElements.define(TAG, CommerceChatElement);

// ---------------------------------------------------------------------------
// Imperative API for merchants (window.CommerceChat)
// ---------------------------------------------------------------------------
let instance: CommerceChatElement | null = null;

export function init(options: WidgetOptions): void {
  if (instance?.isConnected) return; // one widget per page
  const el = document.createElement(TAG) as CommerceChatElement;
  el.options = options;
  document.body.appendChild(el);
  instance = el;
}

export function open(): void {
  instance?.open();
}

export function close(): void {
  instance?.close();
}

export function destroy(): void {
  instance?.remove();
  instance = null;
}

// Auto-initialise from the <script data-*> attributes when present.
const script = document.currentScript as HTMLScriptElement | null;
if (script?.dataset.publishableKey) {
  const boot = () => init({ ...optionsFromDataset(script.dataset) } as WidgetOptions);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, { once: true });
  else boot();
}
