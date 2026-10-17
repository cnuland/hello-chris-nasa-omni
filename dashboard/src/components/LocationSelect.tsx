import type { LocationOption } from '../lib/locations';

const INDENT = '   ';

/** Hierarchical location <option>s (depth-first, indented by depth). */
export function LocationOptions({ options }: { options: LocationOption[] }) {
  return (
    <>
      {options.map((o) => (
        <option key={o.location_id} value={o.location_id}>
          {`${INDENT.repeat(o.depth)}${o.display_name} (${o.location_id})`}
        </option>
      ))}
    </>
  );
}
