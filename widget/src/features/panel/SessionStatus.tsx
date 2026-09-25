import { AlertCircle } from 'lucide-react';
import { startWidget } from '@/app/bootstrap';
import { useAppDispatch } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { Button } from '@/shared/ui/Button';

export function SessionLoading() {
  return (
    <div aria-busy="true" aria-label={strings.sessionLoading} className="flex flex-1 flex-col gap-4 p-4">
      {[70, 45, 60].map((w, i) => (
        <div key={i} className={i % 2 ? 'flex justify-end' : 'flex gap-2'}>
          {i % 2 === 0 && <div className="size-7 animate-pulse rounded-full bg-zinc-200 dark:bg-zinc-800" />}
          <div className="h-10 animate-pulse rounded-2xl bg-zinc-200 dark:bg-zinc-800" style={{ width: `${w}%` }} />
        </div>
      ))}
    </div>
  );
}

export function SessionError() {
  const dispatch = useAppDispatch();
  return (
    <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
      <AlertCircle className="size-8 text-rose-500" aria-hidden="true" />
      <p className="text-sm text-zinc-600 dark:text-zinc-400">{strings.sessionError}</p>
      <Button variant="secondary" onClick={() => void dispatch(startWidget())}>
        {strings.retry}
      </Button>
    </div>
  );
}
