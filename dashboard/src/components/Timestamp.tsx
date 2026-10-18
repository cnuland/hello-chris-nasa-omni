// AC-UI-11: absolute UTC time with the relative time beside it, both visible (not hover-only).
import { formatRelative, formatUtc, formatUtcMinute } from '../lib/format';

interface Props {
  iso: string | null | undefined;
  /** Text when the time is missing (default "never"). */
  empty?: string;
  /** Stack the relative time under the absolute one (table cells). */
  stacked?: boolean;
}

export function Timestamp({ iso, empty = 'never', stacked = false }: Props) {
  const abs = formatUtcMinute(iso);
  if (!iso || !abs) return <span className="muted">{iso ? 'unknown' : empty}</span>;
  const full = formatUtc(iso);
  return (
    <span className={stacked ? 'ts ts-stacked' : 'ts'}>
      <time dateTime={iso} title={full}>{abs}</time>
      <span className="ts-rel muted" title={full}>{formatRelative(iso)}</span>
    </span>
  );
}
