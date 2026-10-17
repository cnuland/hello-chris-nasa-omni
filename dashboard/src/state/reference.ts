// Shared reference data (locations, catalog) loaded once per page that needs it.
import { useEffect, useMemo, useState } from 'react';
import { listCatalog, listLocations } from '../api/client';
import type { CatalogItem, Location } from '../api/types';
import { flattenLocationTree, type LocationOption } from '../lib/locations';
import { useToasts } from './toasts';

export function useLocations(): { locations: Location[]; options: LocationOption[]; nameOf: (id?: string | null) => string } {
  const toasts = useToasts();
  const [locations, setLocations] = useState<Location[]>([]);
  useEffect(() => {
    let live = true;
    listLocations()
      .then((l) => live && setLocations(l))
      .catch((e) => live && toasts.error('Could not load locations', e));
    return () => {
      live = false;
    };
  }, [toasts]);
  return useMemo(() => {
    const byId = new Map(locations.map((l) => [l.location_id, l.display_name]));
    return {
      locations,
      options: flattenLocationTree(locations),
      nameOf: (id?: string | null) => (id ? byId.get(id) ?? '' : ''),
    };
  }, [locations]);
}

export function useCatalog(): CatalogItem[] {
  const toasts = useToasts();
  const [catalog, setCatalog] = useState<CatalogItem[]>([]);
  useEffect(() => {
    let live = true;
    listCatalog()
      .then((c) => live && setCatalog(c))
      .catch((e) => live && toasts.error('Could not load catalog', e));
    return () => {
      live = false;
    };
  }, [toasts]);
  return catalog;
}
