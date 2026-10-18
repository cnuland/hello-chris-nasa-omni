import type { Item } from '../api/types';
import { capitalize, eventActor, eventVerb, formatUtcMinute } from '../lib/format';

const DASH = '—';

/**
 * AC-UI-12: "Used by astronaut in blue suit" + the event time, from `last_handled` (moved / used / handed over...).
 * Falls back to last_moved_by / last_moved_at for APIs without last_handled. UI2-10: operator actions read
 * "Moved by operator ui-review-2" (actor_kind / source, or an actor equal to location_set_by when neither is sent).
 */
export function LastHandledCell({ item }: { item: Item }) {
  const h = item.last_handled;
  const type = h ? h.event_type : item.last_moved_by || item.last_moved_at ? 'moved' : null;
  if (!type) return <span className="muted">{DASH}</span>;
  const actor = h ? h.actor_desc : item.last_moved_by;
  const at = h ? h.occurred_at : item.last_moved_at;
  const setBy = item.location_set_by?.trim();
  const kind = h?.actor_kind ?? (!h?.source && setBy && actor?.trim() === setBy ? 'operator' : undefined);
  const utc = formatUtcMinute(at);
  return (
    <span className="handled">
      <span>{`${capitalize(eventVerb(type))} by ${eventActor({ actor_desc: actor, actor_kind: kind, source: h?.source })}`}</span>
      {utc && <time className="muted" dateTime={at ?? undefined}>{utc}</time>}
    </span>
  );
}
