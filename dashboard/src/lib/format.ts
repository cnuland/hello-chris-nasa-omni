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

/** "2026-10-17 21:42 UTC" (minute precision for lists and cards; empty for missing/invalid input). AC-UI-11. */
export function formatUtcMinute(iso: string | null | undefined): string {
  const full = formatUtc(iso);
  return full ? `${full.slice(0, 16)} UTC` : '';
}

/** First 8 characters of an id ("163e1c50"). */
export function shortId(id: string | null | undefined): string {
  return (id ?? '').slice(0, 8);
}

/** "Node 1 Unity (NOD1)"; just the code when the name is unknown; '' when there is no location. */
export function locationLabel(code: string | null | undefined, name?: string | null): string {
  if (!code) return name?.trim() || '';
  const n = name?.trim();
  return n && n !== code ? `${n} (${code})` : code;
}

const PERSON_HEADS = new Set([
  'a', 'an', 'the', 'person', 'people', 'crew', 'crewmember', 'crewmate', 'astronaut', 'cosmonaut', 'man', 'woman',
  'someone', 'operator', 'pipeline', 'engineer', 'commander', 'member', 'human', 'figure', 'individual', 'user',
  'he', 'she', 'they', 'robot', 'astrobee',
]);
const QUALIFIER_HEADS = new Set(['wearing', 'in', 'with', 'holding', 'dressed', 'carrying', 'using', 'having']);

/**
 * Turns the pipeline's free-text actor description into a phrase that reads after "by" (AC-UI-12):
 *   "astronaut in blue suit" -> unchanged; "wearing a blue tank top" -> "person wearing a blue tank top";
 *   "Blue shirt, black shorts" -> "person (blue shirt, black shorts)"; empty -> "an unknown person".
 * Plain text only: callers render it as a React text node, never as HTML.
 */
export function actorPhrase(desc: string | null | undefined): string {
  const d = (desc ?? '').replace(/\s+/g, ' ').trim().replace(/[.;:,]+$/, '');
  if (!d) return 'an unknown person';
  const lowerFirst = /^[A-Z][a-z]/.test(d) ? d[0].toLowerCase() + d.slice(1) : d;
  const head = lowerFirst.split(/[\s,]/, 1)[0].toLowerCase();
  if (PERSON_HEADS.has(head)) return lowerFirst;
  if (QUALIFIER_HEADS.has(head)) return `person ${lowerFirst}`;
  return `person (${lowerFirst})`;
}

/** Event type -> past-tense verb: moved -> "moved", handed_over -> "handed over", picked_up -> "picked up". */
export function eventVerb(type: string | null | undefined): string {
  const t = (type ?? '').trim().toLowerCase();
  if (!t) return 'handled';
  return humanize(t);
}

export function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

/** "Used by astronaut in blue suit" (no time; callers render the time as a <time>). */
export function handledPhrase(type: string | null | undefined, actor: string | null | undefined): string {
  return `${capitalize(eventVerb(type))} by ${actorPhrase(actor)}`;
}
