import type { Money } from '@/shared/api/contracts';

const formatters = new Map<string, Intl.NumberFormat>();

function formatterFor(locale: string, currency: string): Intl.NumberFormat {
  const key = `${locale}|${currency}`;
  let fmt = formatters.get(key);
  if (!fmt) {
    fmt = new Intl.NumberFormat(locale, { style: 'currency', currency });
    formatters.set(key, fmt);
  }
  return fmt;
}

/**
 * Display-only formatting of a backend-provided amount.
 * The number of minor-unit digits comes from Intl's CLDR data for the currency
 * (JPY 0, USD 2, KWD 3...), which matches ISO 4217 for every currency we support.
 * The widget never adds, rounds or converts money: it only formats it.
 */
export function formatMoney(money: Money, locale = 'en-US'): string {
  const fmt = formatterFor(locale, money.currency);
  const digits = fmt.resolvedOptions().maximumFractionDigits ?? 2;
  return fmt.format(money.amount_minor / 10 ** digits);
}

/** Formats a discount (sent as a positive amount) as a reduction, e.g. "−$5.00". */
export function formatDiscount(money: Money, locale?: string): string {
  return `−${formatMoney(money, locale)}`;
}
