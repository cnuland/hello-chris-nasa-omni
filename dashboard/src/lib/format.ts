// Display helpers: relative/UTC time and media URLs.

/** "5 min ago", "2 h ago", "3 d ago", "just now". Empty input renders as "never". */
export function formatRelative(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 'unknown';
  const sec = Math.round((now.getTime() - t) / 1000);
  if (sec < 0) return 'in the future';
  if (sec < 45) return 'just now';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const h = Math.round(sec / 3600);
  if (h < 24) return `${h} h ago`;
  return `${Math.round(sec / 86400)} d ago`;
}

/** "2026-10-17 12:00:00 UTC" (empty string for missing/invalid input). */
export function formatUtc(iso: string | null | undefined): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return '';
  return `${new Date(t).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

/** API media paths ("/media/...") are served through the /api proxy: "/api/media/...". */
export function mediaUrl(path: string | null | undefined): string | null {
  if (!path) return null;
  if (/^(https?:|blob:|data:)/i.test(path) || path.startsWith('/api/')) return path;
  return `/api${path.startsWith('/') ? '' : '/'}${path}`;
}

/** ISO-8601 UTC -> value for <input type="datetime-local"> (interpreted as UTC). */
export function isoToLocalInput(iso: string | undefined): string {
  if (!iso) return '';
  const t = Date.parse(iso);
  return Number.isNaN(t) ? '' : new Date(t).toISOString().slice(0, 16);
}

/** <input type="datetime-local"> value (treated as UTC) -> ISO-8601 UTC, or undefined when empty/invalid. */
export function localInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const t = Date.parse(`${value}Z`);
  return Number.isNaN(t) ? undefined : new Date(t).toISOString();
}

/** "unknown_object" -> "unknown object". */
export function humanize(s: string | null | undefined): string {
  return (s ?? '').replace(/_/g, ' ');
}
