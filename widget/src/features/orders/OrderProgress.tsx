import { Check } from 'lucide-react';
import { cn } from '@/shared/lib/cn';
import { ORDER_STEPS } from './orderStatus';

/** Happy-path stepper: Confirmed → Payment → Preparing → Complete. */
export function OrderProgress({ step }: { step: number }) {
  return (
    <ol className="flex items-start" aria-label="Order progress">
      {ORDER_STEPS.map((label, i) => {
        const done = i < step || step === ORDER_STEPS.length - 1;
        const current = i === step && !done;
        return (
          <li key={label} className="flex flex-1 flex-col items-center gap-1 text-center" aria-current={current ? 'step' : undefined}>
            <div className="flex w-full items-center">
              <span className={cn('h-0.5 flex-1', i === 0 ? 'invisible' : i <= step ? 'bg-brand' : 'bg-zinc-200 dark:bg-zinc-700')} />
              <span
                className={cn(
                  'flex size-5 items-center justify-center rounded-full text-[10px] font-semibold',
                  done && 'bg-brand text-brand-fg',
                  current && 'bg-brand/15 text-brand ring-2 ring-brand',
                  !done && !current && 'bg-zinc-200 text-zinc-500 dark:bg-zinc-700',
                )}
              >
                {done ? <Check className="size-3" aria-hidden="true" /> : i + 1}
              </span>
              <span
                className={cn(
                  'h-0.5 flex-1',
                  i === ORDER_STEPS.length - 1 ? 'invisible' : i < step ? 'bg-brand' : 'bg-zinc-200 dark:bg-zinc-700',
                )}
              />
            </div>
            <span className={cn('text-[10px]', current || done ? 'text-zinc-700 dark:text-zinc-300' : 'text-zinc-400')}>
              {label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
