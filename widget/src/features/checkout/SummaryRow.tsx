import type { ReactNode } from 'react';
import { cn } from '@/shared/lib/cn';

export function SummaryRow({
  label,
  value,
  emphasis = false,
  tone = 'default',
}: {
  label: ReactNode;
  value: ReactNode;
  emphasis?: boolean;
  tone?: 'default' | 'positive';
}) {
  return (
    <div
      className={cn(
        'flex items-baseline justify-between gap-4',
        emphasis ? 'text-base font-semibold text-zinc-900 dark:text-zinc-50' : 'text-sm text-zinc-600 dark:text-zinc-400',
        tone === 'positive' && 'text-emerald-700 dark:text-emerald-400',
      )}
    >
      <dt>{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}
