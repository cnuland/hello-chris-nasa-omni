// Deterministic fixtures shaped exactly like contracts/openapi/inventory-api.yaml responses.
import type { CatalogItem, Event, Item, ItemDetail, Location, ReviewItem, Stats, TimelineEntry } from '../src/api/types';

/** Frozen "now" used by time-dependent tests. */
export const NOW = new Date('2026-10-17T12:05:00Z');

export const ID = {
  drill: '11111111-1111-4111-8111-111111111111',
  camera: '22222222-2222-4222-8222-222222222222',
  crew: '33333333-3333-4333-8333-333333333333',
  bag: '44444444-4444-4444-8444-444444444444',
  newItem: '55555555-5555-4555-8555-555555555555',
  rvUnknown: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  rvDup: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
  rvLow: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc',
  window1: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
};

export const drill: Item = {
  entity_id: ID.drill,
  catalog_id: 'drill-pistol-grip',
  catalog_name: 'Drill, pistol grip',
  kind: 'object',
  status: 'confirmed',
  label: 'Pistol-grip drill',
  description: 'Orange pistol-grip power tool',
  attributes: { color: 'orange', battery: '18V' },
  last_location: 'LAB1S2',
  last_location_name: 'Lab starboard rack 2',
  last_seen_at: '2026-10-17T12:00:00Z',
  first_seen_at: '2026-10-16T08:00:00Z',
  confidence: 0.87,
  snapshot_url: '/media/snap/drill.jpg',
  needs_review: false,
  review_reason: null,
  last_moved_by: 'crew member, blue shirt',
  last_moved_at: '2026-10-17T11:00:00Z',
};

export const camera: Item = {
  entity_id: ID.camera,
  catalog_id: 'camera-dslr',
  catalog_name: 'Camera, DSLR',
  kind: 'object',
  status: 'provisional',
  label: 'DSLR camera',
  description: 'Black camera with long lens',
  attributes: {},
  last_location: 'NOD2D4',
  last_location_name: 'Node 2 deck bay 4',
  last_seen_at: '2026-10-17T09:05:00Z',
  first_seen_at: '2026-10-17T09:00:00Z',
  confidence: 0.42,
  snapshot_url: null,
  needs_review: true,
  review_reason: 'low_confidence',
  last_moved_by: null,
  last_moved_at: null,
};

export const crew: Item = {
  entity_id: ID.crew,
  catalog_id: null,
  catalog_name: null,
  kind: 'person',
  status: 'provisional',
  label: 'crew member, blue shirt',
  description: null,
  attributes: {},
  last_location: 'LAB1',
  last_location_name: 'Destiny (US Lab)',
  last_seen_at: '2026-10-17T11:50:00Z',
  first_seen_at: '2026-10-17T08:00:00Z',
  confidence: 0.95,
  snapshot_url: '/media/snap/crew.jpg',
  needs_review: false,
  review_reason: null,
  last_moved_by: null,
  last_moved_at: null,
};

export const bag: Item = {
  entity_id: ID.bag,
  catalog_id: 'ctb-half',
  catalog_name: 'Cargo transfer bag (half)',
  kind: 'object',
  status: 'confirmed',
  label: 'Half CTB',
  description: 'White cargo transfer bag',
  attributes: {},
  last_location: 'NOD2O4',
  last_location_name: 'Node 2 overhead bay 4',
  last_seen_at: '2026-10-16T12:05:00Z',
  first_seen_at: '2026-10-10T12:00:00Z',
  confidence: 0.7,
  snapshot_url: '/media/snap/bag.jpg',
  needs_review: false,
  review_reason: null,
  last_moved_by: null,
  last_moved_at: null,
};

export const ITEMS: Item[] = [drill, crew, camera, bag];

export const drillEvents: Event[] = [
  {
    event_id: 'e0000000-0000-4000-8000-000000000001',
    event_type: 'moved',
    entity_id: ID.drill,
    actor_desc: 'crew member, blue shirt',
    from_location: 'NOD2D4',
    to_location: 'LAB1S2',
    occurred_at: '2026-10-17T11:00:00Z',
    confidence: 0.8,
    evidence_url: '/media/ev/1.jpg',
    source: 'pipeline',
  },
  {
    event_id: 'e0000000-0000-4000-8000-000000000002',
    event_type: 'moved',
    entity_id: ID.drill,
    actor_desc: null,
    from_location: 'LAB1P3',
    to_location: 'NOD2D4',
    occurred_at: '2026-10-16T09:30:00Z',
    confidence: 0.6,
    evidence_url: null,
    source: 'pipeline',
  },
];

export const drillDetail: ItemDetail = {
  ...drill,
  sightings: [{ seen_at: '2026-10-17T12:00:00Z', location_id: 'LAB1S2', confidence: 0.87, frame_url: '/media/f/1.jpg' }],
  events: drillEvents,
  exemplar_count: 4,
};

export const drillHistory: TimelineEntry[] = [
  { at: '2026-10-17T12:00:00Z', kind: 'sighting', location_id: 'LAB1S2', summary: 'Seen on starboard rack 2', image_url: '/media/f/1.jpg' },
  { at: '2026-10-17T11:00:00Z', kind: 'event', location_id: 'LAB1S2', summary: 'Moved from NOD2D4 to LAB1S2', image_url: '/media/ev/1.jpg' },
  { at: '2026-10-16T10:00:00Z', kind: 'audit', location_id: null, summary: 'Label edited by alice', image_url: null },
];

export const LOCATIONS: Location[] = [
  { location_id: 'USOS', parent_id: null, kind: 'segment', display_name: 'US Orbital Segment' },
  { location_id: 'NOD2', parent_id: 'USOS', kind: 'module', display_name: 'Node 2 Harmony' },
  { location_id: 'LAB1', parent_id: 'USOS', kind: 'module', display_name: 'Destiny (US Lab)' },
  // children deliberately listed out of order to prove the hierarchy is built, not just echoed
  { location_id: 'LAB1S2', parent_id: 'LAB1', kind: 'zone', display_name: 'Lab starboard rack 2' },
  { location_id: 'NOD2D4', parent_id: 'NOD2', kind: 'zone', display_name: 'Node 2 deck bay 4' },
  { location_id: 'LAB1P3', parent_id: 'LAB1', kind: 'zone', display_name: 'Lab port rack 3' },
  { location_id: 'NOD2O4', parent_id: 'NOD2', kind: 'zone', display_name: 'Node 2 overhead bay 4' },
];

export const CATALOG: CatalogItem[] = [
  { catalog_id: 'drill-pistol-grip', class_name: 'pistol-grip drill', display_name: 'Drill, pistol grip', category: 'object' },
  { catalog_id: 'camera-dslr', class_name: 'dslr camera', display_name: 'Camera, DSLR', category: 'object' },
  { catalog_id: 'ctb-half', class_name: 'cargo transfer bag', display_name: 'Cargo transfer bag (half)', category: 'container' },
  { catalog_id: 'wipes-pack', class_name: 'wet wipes pack', display_name: 'Wipes pack', category: 'object', is_fungible: true },
];

export const REVIEWS: ReviewItem[] = [
  {
    review_id: ID.rvUnknown,
    kind: 'unknown_object',
    entity_id: null,
    candidate_ids: [],
    candidates: [],
    image_url: '/media/reviews/a.jpg',
    description: 'Small yellow cylinder with a red cap',
    observed_at: '2026-10-17T10:15:00Z',
    location_id: 'NOD2D4',
    model_guess: 'flashlight',
    probabilities: { flashlight: 0.41, other: 0.59 },
    status: 'open',
  },
  {
    review_id: ID.rvDup,
    kind: 'possible_duplicate',
    entity_id: ID.newItem,
    candidate_ids: [ID.drill, ID.camera],
    candidates: [drill, { ...camera, snapshot_url: '/media/snap/camera.jpg' }],
    image_url: '/media/reviews/b.jpg',
    description: 'Orange power tool near rack',
    observed_at: '2026-10-17T11:20:00Z',
    location_id: 'LAB1S2',
    model_guess: 'pistol-grip drill',
    probabilities: { same: 0.55, different: 0.45 },
    status: 'open',
  },
  {
    review_id: ID.rvLow,
    kind: 'low_confidence',
    entity_id: ID.camera,
    candidate_ids: [],
    candidates: [],
    image_url: '/media/reviews/c.jpg',
    description: 'Black object partially occluded',
    observed_at: '2026-10-17T09:05:00Z',
    location_id: 'NOD2D4',
    model_guess: 'dslr camera',
    probabilities: {},
    status: 'open',
  },
];

/**
 * /stats (AC-API-16 shape). Default open-review counts are consistent with REVIEWS (one of each kind = 3 open),
 * so the nav badge is 3 whichever source (GET /review or /stats) the implementation uses.
 */
export const STATS: Stats = {
  by_status: { confirmed: 2, provisional: 2, unknown: 0, merged: 0, dismissed: 0 },
  by_kind: { object: 3, person: 1 },
  by_module: { LAB1: 2, NOD2: 2 },
  open_reviews_by_kind: { unknown_object: 1, possible_duplicate: 1, low_confidence: 1, conflict: 0 },
};

/**
 * Distinct numbers for the overview tiles (AC-UI-9 semantics, decided in the red phase):
 *   Items = sum(by_status) excluding merged + dismissed          -> 42 (all statuses = 45)
 *   Unknowns = open_reviews_by_kind.unknown_object               -> 7
 *   Possible duplicates = open_reviews_by_kind.possible_duplicate -> 3
 *   People seen in the last hour = total of GET /items?kind=person&seen_after=<now-1h> (handler returns 2)
 */
export const OVERVIEW_STATS: Stats = {
  by_status: { confirmed: 30, provisional: 8, unknown: 4, merged: 2, dismissed: 1 },
  // by_kind counts live entities only (status not merged/dismissed): 37 + 5 = 42 = live by_status total.
  by_kind: { object: 37, person: 5 },
  by_module: { LAB1: 20, NOD2: 25 },
  open_reviews_by_kind: { unknown_object: 7, possible_duplicate: 3, low_confidence: 1, conflict: 0 },
};

export const RECENT_EVENTS: Event[] = [
  {
    event_id: 'e0000000-0000-4000-8000-000000000010',
    event_type: 'moved',
    entity_id: ID.drill,
    actor_desc: 'crew member, blue shirt',
    from_location: 'NOD2D4',
    to_location: 'LAB1S2',
    occurred_at: '2026-10-17T11:00:00Z',
    confidence: 0.8,
    evidence_url: '/media/ev/1.jpg',
    source: 'pipeline',
  },
  {
    event_id: 'e0000000-0000-4000-8000-000000000011',
    event_type: 'added',
    entity_id: ID.bag,
    actor_desc: 'operator',
    from_location: null,
    to_location: 'NOD2O4',
    occurred_at: '2026-10-17T10:00:00Z',
    confidence: 1,
    evidence_url: null,
    source: 'operator',
  },
];

export function makeItems(n: number, offset = 0): Item[] {
  return Array.from({ length: n }, (_, i) => {
    const k = offset + i;
    const hex = k.toString(16).padStart(12, '0');
    return {
      ...bag,
      entity_id: `90000000-0000-4000-8000-${hex}`,
      label: `Bulk item ${k + 1}`,
    };
  });
}
