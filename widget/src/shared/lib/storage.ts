/**
 * Storage that never throws. Storage can be blocked or unavailable
 * (private mode, strict cookie settings, sandboxed iframes). The widget must
 * still work without it, only losing session continuity.
 */
export const safeStorage = {
  read<T>(key: string): T | null {
    try {
      const raw = window.localStorage.getItem(key);
      return raw ? (JSON.parse(raw) as T) : null;
    } catch {
      return null;
    }
  },
  write(key: string, value: unknown): void {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* storage unavailable: continue without persistence */
    }
  },
  remove(key: string): void {
    try {
      window.localStorage.removeItem(key);
    } catch {
      /* ignore */
    }
  },
};
