import { Sparkles } from 'lucide-react';
import { useAppSelector } from '@/app/hooks';
import { cn } from '@/shared/lib/cn';

export function AssistantAvatar({ className }: { className?: string }) {
  const logo = useAppSelector((s) => s.session.merchant?.logo_url);
  return (
    <span
      aria-hidden="true"
      className={cn('flex size-7 shrink-0 items-center justify-center overflow-hidden rounded-full bg-brand text-brand-fg', className)}
    >
      {logo ? <img src={logo} alt="" className="size-full object-cover" /> : <Sparkles className="size-3.5" />}
    </span>
  );
}
