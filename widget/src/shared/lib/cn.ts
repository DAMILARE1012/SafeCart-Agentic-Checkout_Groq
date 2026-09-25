import { clsx, type ClassValue } from 'clsx';

/** Conditional className helper. */
export function cn(...values: ClassValue[]): string {
  return clsx(values);
}
