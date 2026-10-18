import type { Item } from '../api/types';
import { capitalize, eventActor, eventVerb, formatUtcMinute, locationLabel } from '../lib/format';
import { isPerson } from '../lib/person';

const DASH = '—';

/** UI5-09: a pipeline move this close to first_seen_at, with no actor, is the item's first sighting. */
export const FIRST_SIGHTING_MS = 5000;

/**
 * AC-UI-12: "Used by astronaut in blue suit" + the event time, from `last_handled` (moved / used / handed over...).
 * Falls back to last_moved_by / last_moved_at for APIs without last_handled. UI2-10: operator actions read
 * "Moved by operator ui-review-2" (actor_kind / source, or an actor equal to location_set_by when neither is sent).
 * UI5-09: person tracks read "Last seen in <place> · time" (never "handled"); a first sighting recorded as a move
 * (no origin, no actor, at first_seen_at) reads "First seen in <place>".
 */
export function LastHandledCell({ item }: { item: Item }) {
  if (isPerson(item)) {
    const place = locationLabel(item.last_location, item.last_location_name);
    const at = item.last_seen_at ?? item.last_handled?.occurred_at ?? null;
    const utc = formatUtcMinute(at);
    return (
      <span className="handled">
        <span>{place ? `Last seen in ${place}` : 'Last seen (place not recorded)'}</span>
        {utc && (
          <>
            <span className="muted" aria-hidden="true">{' · '}</span>
            <time className="muted" dateTime={at ?? undefined}>{utc}</time>
          </>
        )}
      </span>
    );
  }
  const h = item.last_handled;
  const type = h ? h.event_type : item.last_moved_by || item.last_moved_at ? 'moved' : null;
  if (!type) return <span className="muted">{DASH}</span>;
  const actor = h ? h.actor_desc : item.last_moved_by;
  const at = h ? h.occurred_at : item.last_moved_at;
  const setBy = item.location_set_by?.trim();
  const kind = h?.actor_kind ?? (!h?.source && setBy && actor?.trim() === setBy ? 'operator' : undefined);
  const utc = formatUtcMinute(at);
  const operator = kind === 'operator' || ['operator', 'api'].includes((h?.source ?? '').toLowerCase());
  const atMs = Date.parse(at ?? '');
  const firstMs = Date.parse(item.first_seen_at ?? '');
  const first = !!h && type === 'moved' && !actor?.trim() && !operator
    && Number.isFinite(atMs) && Number.isFinite(firstMs) && Math.abs(atMs - firstMs) <= FIRST_SIGHTING_MS;
  let text: string;
  if (first) {
    const place = locationLabel(item.last_location, item.last_location_name);
    text = place ? `First seen in ${place}` : 'First seen';
  } else {
    text = `${capitalize(eventVerb(type))} by ${eventActor({ actor_desc: actor, actor_kind: kind, source: h?.source })}`;
  }
  return (
    <span className="handled">
      <span>{text}</span>
      {utc && <time className="muted" dateTime={at ?? undefined}>{utc}</time>}
    </span>
  );
}
