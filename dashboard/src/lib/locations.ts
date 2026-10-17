import type { Location } from '../api/types';

export interface LocationOption {
  location_id: string;
  display_name: string;
  depth: number;
}

/**
 * Depth-first ordering of the location hierarchy (parent before children). Siblings keep the API order. Orphans
 * (unknown parent) are treated as roots, and cycles are broken, so no location is ever dropped.
 */
export function flattenLocationTree(locations: Location[]): LocationOption[] {
  const ids = new Set(locations.map((l) => l.location_id));
  const children = new Map<string | null, Location[]>();
  for (const l of locations) {
    const parent = l.parent_id && ids.has(l.parent_id) && l.parent_id !== l.location_id ? l.parent_id : null;
    const list = children.get(parent) ?? [];
    list.push(l);
    children.set(parent, list);
  }
  const out: LocationOption[] = [];
  const visited = new Set<string>();
  const walk = (parent: string | null, depth: number) => {
    for (const l of children.get(parent) ?? []) {
      if (visited.has(l.location_id)) continue;
      visited.add(l.location_id);
      out.push({ location_id: l.location_id, display_name: l.display_name, depth });
      walk(l.location_id, depth + 1);
    }
  };
  walk(null, 0);
  // Anything only reachable through a cycle: append as roots.
  for (const l of locations) {
    if (!visited.has(l.location_id)) {
      visited.add(l.location_id);
      out.push({ location_id: l.location_id, display_name: l.display_name, depth: 0 });
      walk(l.location_id, 1);
    }
  }
  return out;
}
