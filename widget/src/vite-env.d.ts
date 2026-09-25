/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Default gateway URL baked into the bundle (overridable per embed). */
  readonly VITE_GATEWAY_BASE_URL?: string;
  /** Dev playground only: publishable key used by index.html. */
  readonly VITE_WIDGET_PUBLISHABLE_KEY?: string;
  /** Dev playground only: "true" serves the gateway API from Mock Service Worker. */
  readonly VITE_USE_MOCKS?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

declare module '*.css?inline' {
  const css: string;
  export default css;
}
