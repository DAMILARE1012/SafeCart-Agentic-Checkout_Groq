import { AlertTriangle, Info, XCircle } from 'lucide-react';
import { cn } from '@/shared/lib/cn';

const styles = {
  info: { icon: Info, className: 'border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-900 dark:bg-sky-950/50 dark:text-sky-200' },
  warning: {
    icon: AlertTriangle,
    className: 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-200',
  },
  error: { icon: XCircle, className: 'border-rose-200 bg-rose-50 text-rose-900 dark:border-rose-900 dark:bg-rose-950/50 dark:text-rose-200' },
} as const;

export function NoticeBlock({ level, text }: { level: keyof typeof styles; text: string }) {
  const { icon: Icon, className } = styles[level];
  return (
    <div role={level === 'error' ? 'alert' : 'status'} className={cn('flex gap-2 rounded-xl border px-3 py-2 text-sm', className)}>
      <Icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <p>{text}</p>
    </div>
  );
}
