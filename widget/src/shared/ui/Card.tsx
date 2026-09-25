import type { HTMLAttributes } from 'react';
import { cn } from '@/shared/lib/cn';

/** Surface used by every rich block (products, cart, quote, order). */
export function Card({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'rounded-2xl border border-zinc-200 bg-white shadow-sm dark:border-zinc-700/80 dark:bg-zinc-800/60',
        className,
      )}
      {...rest}
    />
  );
}
