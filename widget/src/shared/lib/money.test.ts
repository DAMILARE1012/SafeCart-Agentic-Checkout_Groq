import { describe, expect, it } from 'vitest';
import { formatDiscount, formatMoney } from './money';

describe('formatMoney', () => {
  it('formats two-decimal currencies from minor units', () => {
    expect(formatMoney({ amount_minor: 1999, currency: 'USD' }, 'en-US')).toBe('$19.99');
  });

  it('formats zero-decimal currencies without dividing', () => {
    expect(formatMoney({ amount_minor: 1200, currency: 'JPY' }, 'en-US')).toBe('¥1,200');
  });

  it('respects the locale', () => {
    // de-DE uses a non-breaking space before the symbol
    expect(formatMoney({ amount_minor: 8460, currency: 'EUR' }, 'de-DE')).toBe('84,60 €');
  });

  it('formats discounts as reductions', () => {
    expect(formatDiscount({ amount_minor: 500, currency: 'USD' }, 'en-US')).toBe('−$5.00');
  });
});
