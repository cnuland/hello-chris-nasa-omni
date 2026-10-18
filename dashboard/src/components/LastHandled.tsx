import type { Item } from '../api/types';
import { formatUtcMinute, handledPhrase } from '../lib/format';

const DASH = '—';

/**
 * AC-UI-12: "Used by astronaut in blue suit" + the event time, from `last_handled` (moved / used / handed over...).
 * Falls back to last_moved_by / last_moved_at for APIs without last_handled.
 */
export function LastHandledCell({ item }: { item: Item }) {
  const h = item.last_handled;
  const type = h ? h.event_type : item.last_moved_by || item.last_moved_at ? 'moved' : null;
  if (!type) return <span className="muted">{DASH}</span>;
  const actor = h ? h.actor_desc : item.last_moved_by;
  const at = h ? h.occurred_at : item.last_moved_at;
  const utc = formatUtcMinute(at);
  return (
    <span className="handled">
      <span>{handledPhrase(type, actor)}</span>
      {utc && <time className="muted" dateTime={at ?? undefined}>{utc}</time>}
    </span>
  );
}

