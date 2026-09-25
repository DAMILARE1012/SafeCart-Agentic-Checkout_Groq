import { useEffect, useState } from 'react';

const secondsUntil = (target: number) =>
  Number.isNaN(target) ? 0 : Math.max(0, Math.round((target - Date.now()) / 1000));

/** Seconds remaining until `expiresAt` (ISO string), ticking once per second and stopping at 0. */
export function useCountdown(expiresAt: string | null | undefined): number {
  const target = expiresAt ? Date.parse(expiresAt) : Number.NaN;
  const [seconds, setSeconds] = useState(() => secondsUntil(target));

  useEffect(() => {
    setSeconds(secondsUntil(target));
    if (Number.isNaN(target)) return;
    const timer = setInterval(() => {
      const next = secondsUntil(target);
      setSeconds(next);
      if (next === 0) clearInterval(timer);
    }, 1000);
    return () => clearInterval(timer);
  }, [target]);

  return seconds;
}

export function formatCountdown(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
