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

// ---------------------------------------------------------------- event wording (UI2-09 / UI2-10 / UI-04 / UI-13)
/** The event fields the shared wording needs; every field is optional. */
export interface EventWording {
  event_type?: string | null;
  actor_desc?: string | null;
  /** "operator" | "person" from the API; when absent, `source` decides (operator/api = an operator). */
  actor_kind?: string | null;
  source?: string | null;
  from_location?: string | null;
  to_location?: string | null;
  from_location_name?: string | null;
  to_location_name?: string | null;
  /** "at" (one place) | "from_to" (a route) from the API; when absent, moves are routes and everything else is "at". */
  location_role?: string | null;
  /** UI3-04: the subject's catalog entry / kind; "crew-member" / "person" marks a person's own track event. */
  entity_catalog_id?: string | null;
  entity_kind?: string | null;
  /** UI4-06: the subject's current location (optional). */
  entity_last_location?: string | null;
  entity_last_location_name?: string | null;
}

const OPERATOR_SOURCES = new Set(['operator', 'api']);

/** "operator ui-review-2" for operator actions, else the person phrasing of actorPhrase (AC-UI-12). */
export function eventActor(e: Pick<EventWording, 'actor_desc' | 'actor_kind' | 'source'>): string {
  const kind = (e.actor_kind ?? '').toLowerCase();
  const isOperator = kind ? kind === 'operator' : OPERATOR_SOURCES.has((e.source ?? '').toLowerCase());
  if (!isOperator) return actorPhrase(e.actor_desc);
  const d = (e.actor_desc ?? '').replace(/\s+/g, ' ').trim();
  if (!d) return 'an operator';
  if (/^operator\b/i.test(d)) return `operator${d.slice('operator'.length)}`;
  return `operator ${d}`;
}

/**
 * Where the event happened: "at X" for uses, additions and other one-place events; "from A to B" for moves (and,
 * without location_role, for any event whose two ends differ).
 * A move with no destination says "from X (destination not seen)" (UI3-04). '' when no location is known.
 */
export function eventPlace(e: EventWording): string {
  const from = e.from_location ? locationLabel(e.from_location, e.from_location_name) : '';
  const to = e.to_location ? locationLabel(e.to_location, e.to_location_name) : '';
  const type = (e.event_type ?? '').toLowerCase();
  const twoPlaces = !!(from && to && e.from_location !== e.to_location);
  const role = e.location_role === 'at' || e.location_role === 'from_to'
    ? e.location_role
    : type === 'moved' || twoPlaces ? 'from_to' : 'at';
  if (role === 'at' || (from && to && e.from_location === e.to_location)) {
    const at = to || from;
    return at ? `at ${at}` : '';
  }
  if (from && to) return `from ${from} to ${to}`;
  if (from) return type === 'moved' ? `from ${from} (destination not seen)` : `from ${from}`;
  if (to) return `to ${to}`;
  return '';
}

/** UI3-04: the event's subject is a person (crew-member catalog entry, entity_kind, or the known item kind). */
export function isPersonEvent(e: Pick<EventWording, 'entity_catalog_id' | 'entity_kind'>, itemKind?: string | null): boolean {
  return e.entity_catalog_id === 'crew-member' || e.entity_kind === 'person' || itemKind === 'person';
}

/** Where the subject is now: the event's entity_last_location, else a caller-supplied one (e.g. the cached item). */
export interface LastLocation {
  lastLocation?: string | null;
  lastLocationName?: string | null;
}

function lastLocationOf(e: EventWording, opts: LastLocation): { code: string; label: string } | null {
  const code = e.entity_last_location || opts.lastLocation || '';
  if (!code) return null;
  const name = e.entity_last_location ? e.entity_last_location_name : opts.lastLocationName;
  return { code, label: locationLabel(code, name) };
}

/**
 * UI3-04 / UI4-06: a person's own track event, with no actor (the person is the subject, not someone moving it):
 * "seen in Cupola (CUP1)", "left Node 1 Unity (NOD1)", "arrived in …", "moved from A to B".
 * Always a place: "seen" with no place uses the subject's last location, else "seen (place not seen)". "left X"
 * reads "seen in X" when the subject's last location is still X.
 */
export function personEventSentence(e: EventWording, opts: LastLocation = {}): string {
  const from = e.from_location ? locationLabel(e.from_location, e.from_location_name) : '';
  const to = e.to_location ? locationLabel(e.to_location, e.to_location_name) : '';
  const type = (e.event_type ?? '').toLowerCase();
  const route = e.location_role === 'from_to' || (e.location_role !== 'at' && type === 'moved');
  const last = lastLocationOf(e, opts);
  if (route && from && to && e.from_location !== e.to_location) return `moved from ${from} to ${to}`;
  if (route && from && !to) return last?.code === e.from_location ? `seen in ${from}` : `left ${from}`;
  if (route && to && !from) return `arrived in ${to}`;
  const at = to || from || last?.label;
  return at ? `seen in ${at}` : 'seen (place not seen)';
}

/**
 * UI4-06: an object event for the overview feed, always with a place:
 * "used in Node 1 Unity (NOD1) by person (grey t-shirt)", "moved from A to B by operator x",
 * "used (last seen in X) by …" when the event has no place, "moved (place not seen)" when nothing is known.
 */
export function objectEventSentence(e: EventWording): string {
  const place = eventPlace(e).replace(/^at /, 'in ');
  const last = lastLocationOf(e, {});
  const where = place || (last ? `(last seen in ${last.label})` : '(place not seen)');
  const who = eventActor(e);
  // A person with no description adds nothing ("by an unknown person"); an operator is always named (UI2-10).
  const actor = e.actor_desc?.trim() || who === 'an operator' ? ` by ${who}` : '';
  return `${eventVerb(e.event_type)} ${where}${actor}`;
}

/**
 * "Used by person (blue shirt) at US Orbital Segment (USOS)", "Moved by operator ui-review-2 from A to B".
 * With `person` (the subject is a person, UI3-04): "Left Node 1 Unity (NOD1)", never "by <actor>".
 */
export function eventSentence(e: EventWording, opts: { person?: boolean } & LastLocation = {}): string {
  if (opts.person || isPersonEvent(e)) return capitalize(personEventSentence(e, opts));
  const place = eventPlace(e);
  return `${capitalize(eventVerb(e.event_type))} by ${eventActor(e)}${place ? ` ${place}` : ''}`;
}

const LEGACY_SUMMARY = /^([a-z][a-z_]*)(?: (\S+) -> (\S+))?(?: by (.+))?$/;

/**
 * Rewrites the inventory-api history summary for events ("used USOS -> ? by Blue shirt") into the shared wording.
 * Returns null for any other shape, so the caller keeps the original text.
 */
export function legacyEventSummary(
  summary: string | null | undefined,
  extra: Pick<EventWording, 'actor_kind' | 'source' | 'location_role'> = {},
  opts: { person?: boolean } = {},
): string | null {
  const m = LEGACY_SUMMARY.exec((summary ?? '').trim());
  if (!m) return null;
  const loc = (v?: string) => (v && v !== '?' ? v : null);
  return eventSentence({
    ...extra, event_type: m[1], from_location: loc(m[2]), to_location: loc(m[3]), actor_desc: m[4] ?? null,
  }, opts);
}
