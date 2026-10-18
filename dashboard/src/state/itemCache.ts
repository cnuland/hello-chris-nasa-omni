// Small in-memory cache of GET /items/{id} for chat citation chips and proposal cards (AC-UI-20). Entries live for
// ITEM_CACHE_TTL_MS; failures are cached as null for the same time so a missing item is not re-requested per render.
import { useEffect, useState } from 'react';
import { getItem } from '../api/client';
import type { ItemDetail } from '../api/types';
import { UUID_RE } from '../lib/agent';

export const ITEM_CACHE_TTL_MS = 60_000;
const MAX_ENTRIES = 200;
const cache = new Map<string, { at: number; value: Promise<ItemDetail | null> }>();

export function clearItemCache(): void {
  cache.clear();
}

export function cachedItem(id: string): Promise<ItemDetail | null> {
  const now = Date.now();
  const hit = cache.get(id);
  if (hit && now - hit.at < ITEM_CACHE_TTL_MS) return hit.value;
  if (cache.size >= MAX_ENTRIES) cache.delete(cache.keys().next().value as string);
  const value = UUID_RE.test(id) ? getItem(id).catch(() => null) : Promise.resolve(null);
  cache.set(id, { at: now, value });
  return value;
}

/** The item (or null while loading / when it cannot be loaded). */
export function useCachedItem(id: string | null | undefined): ItemDetail | null {
  const [item, setItem] = useState<ItemDetail | null>(null);
  useEffect(() => {
    if (!id) return undefined;
    let live = true;
    void cachedItem(id).then((it) => live && setItem(it));
    return () => {
      live = false;
    };
  }, [id]);
  return item;
}
