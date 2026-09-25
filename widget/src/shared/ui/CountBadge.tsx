import { cn } from '@/shared/lib/cn';

/** Small numeric badge (unread messages, cart items). Hidden from assistive tech: the parent's label carries the count. */
export function CountBadge({ count, className }: { count: number; className?: string }) {
  if (count <= 0) return null;
  return (
    <span
      aria-hidden="true"
      className={cn(
        'absolute -top-1 -right-1 flex h-5 min-w-5 items-center justify-center rounded-full px-1',
        'bg-rose-600 text-[11px] leading-none font-semibold text-white ring-2 ring-white dark:ring-zinc-900',
        className,
      )}
    >
      {count > 99 ? '99+' : count}
    </span>
  );
}
