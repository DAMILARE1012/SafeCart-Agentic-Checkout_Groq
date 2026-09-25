import { useAppSelector } from '@/app/hooks';
import type { Money } from '@/shared/api/contracts';
import { formatMoney } from '@/shared/lib/money';

export function PriceTag({ price, compareAt }: { price: Money; compareAt?: Money | null }) {
  const locale = useAppSelector((s) => s.config.locale);
  const onSale = compareAt && compareAt.amount_minor > price.amount_minor;

  return (
    <span className="flex items-baseline gap-1.5">
      <span className={onSale ? 'font-semibold text-rose-600 dark:text-rose-400' : 'font-semibold'}>
        {formatMoney(price, locale)}
      </span>
      {onSale && (
        <span className="text-xs text-zinc-400 line-through">
          <span className="sr-only">Was </span>
          {formatMoney(compareAt, locale)}
        </span>
      )}
    </span>
  );
}
