import { useEffect, useState } from 'react';
import type { ThemeMode } from './config';

/** Resolves 'auto' against the OS preference and follows changes live. */
export function useResolvedTheme(mode: ThemeMode): 'light' | 'dark' {
  const query = '(prefers-color-scheme: dark)';
  const [prefersDark, setPrefersDark] = useState(() => window.matchMedia?.(query).matches ?? false);

  useEffect(() => {
    if (mode !== 'auto' || !window.matchMedia) return;
    const mql = window.matchMedia(query);
    const onChange = (e: MediaQueryListEvent) => setPrefersDark(e.matches);
    mql.addEventListener('change', onChange);
    return () => mql.removeEventListener('change', onChange);
  }, [mode]);

  if (mode === 'auto') return prefersDark ? 'dark' : 'light';
  return mode;
}
