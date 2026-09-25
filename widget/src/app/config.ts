export type ThemeMode = 'light' | 'dark' | 'auto';
export type LauncherPosition = 'bottom-right' | 'bottom-left';

/** Everything a merchant can configure when embedding the widget. */
export interface WidgetConfig {
  publishableKey: string;
  gatewayBaseUrl: string;
  locale: string;
  currency: string | null;
  theme: ThemeMode;
  position: LauncherPosition;
  brandColor: string;
  brandForeground: string;
  merchantName: string | null;
  openOnLoad: boolean;
}

export type WidgetOptions = Partial<WidgetConfig> & { publishableKey: string };

const HEX_COLOR = /^#(?:[0-9a-f]{3}){1,2}$/i;

function pick<T extends string>(value: string | undefined | null, allowed: readonly T[], fallback: T): T {
  return value && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;
}

/** Normalises and validates merchant-supplied options. Throws on unusable config. */
export function resolveConfig(options: WidgetOptions): WidgetConfig {
  const gatewayBaseUrl = (options.gatewayBaseUrl ?? import.meta.env.VITE_GATEWAY_BASE_URL ?? '').replace(/\/+$/, '');
  if (!options.publishableKey) throw new Error('[CommerceChat] publishableKey is required');
  if (!gatewayBaseUrl) throw new Error('[CommerceChat] gatewayBaseUrl is required');

  return {
    publishableKey: options.publishableKey,
    gatewayBaseUrl,
    locale: options.locale ?? navigator.language ?? 'en-US',
    currency: options.currency ?? null,
    theme: pick(options.theme, ['light', 'dark', 'auto'] as const, 'auto'),
    position: pick(options.position, ['bottom-right', 'bottom-left'] as const, 'bottom-right'),
    brandColor: options.brandColor && HEX_COLOR.test(options.brandColor) ? options.brandColor : '#4f46e5',
    brandForeground:
      options.brandForeground && HEX_COLOR.test(options.brandForeground) ? options.brandForeground : '#ffffff',
    merchantName: options.merchantName ?? null,
    openOnLoad: options.openOnLoad ?? false,
  };
}

/** Reads options from `data-*` attributes on the <script> tag or the custom element. */
export function optionsFromDataset(dataset: DOMStringMap): Partial<WidgetOptions> {
  const options: Partial<WidgetOptions> = {};
  if (dataset.publishableKey) options.publishableKey = dataset.publishableKey;
  if (dataset.gatewayUrl) options.gatewayBaseUrl = dataset.gatewayUrl;
  if (dataset.locale) options.locale = dataset.locale;
  if (dataset.currency) options.currency = dataset.currency;
  if (dataset.theme) options.theme = dataset.theme as ThemeMode;
  if (dataset.position) options.position = dataset.position as LauncherPosition;
  if (dataset.brandColor) options.brandColor = dataset.brandColor;
  if (dataset.brandForeground) options.brandForeground = dataset.brandForeground;
  if (dataset.merchantName) options.merchantName = dataset.merchantName;
  if (dataset.openOnLoad) options.openOnLoad = dataset.openOnLoad === 'true';
  return options;
}
