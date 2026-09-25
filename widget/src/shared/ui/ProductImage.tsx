import { ImageOff } from 'lucide-react';
import { useState } from 'react';
import { cn } from '@/shared/lib/cn';

/** Lazy-loaded product image with a neutral fallback when the URL is missing or broken. */
export function ProductImage({ src, alt, className }: { src?: string | null; alt: string; className?: string }) {
  const [failed, setFailed] = useState(false);

  if (!src || failed) {
    return (
      <div
        role="img"
        aria-label={alt}
        className={cn('flex items-center justify-center bg-zinc-100 text-zinc-400 dark:bg-zinc-800', className)}
      >
        <ImageOff className="size-5" aria-hidden="true" />
      </div>
    );
  }

  return (
    <img
      src={src}
      alt={alt}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
      className={cn('bg-zinc-100 object-cover dark:bg-zinc-800', className)}
    />
  );
}
