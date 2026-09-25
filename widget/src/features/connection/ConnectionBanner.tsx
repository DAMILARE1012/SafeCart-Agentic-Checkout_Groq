import { WifiOff } from 'lucide-react';
import { useAppSelector } from '@/app/hooks';
import { strings } from '@/shared/i18n/strings';
import { Spinner } from '@/shared/ui/Spinner';

export function ConnectionBanner() {
  const network = useAppSelector((s) => s.connection.network);
  const events = useAppSelector((s) => s.connection.events);

  if (network === 'offline') {
    return (
      <div role="status" className="flex items-center gap-2 bg-zinc-900 px-4 py-2 text-xs text-white dark:bg-zinc-100 dark:text-zinc-900">
        <WifiOff className="size-3.5" aria-hidden="true" />
        {strings.offlineBanner}
      </div>
    );
  }
  if (events === 'reconnecting') {
    return (
      <div role="status" className="flex items-center gap-2 bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:bg-amber-950/60 dark:text-amber-200">
        <Spinner className="size-3" />
        {strings.reconnectingBanner}
      </div>
    );
  }
  return null;
}
