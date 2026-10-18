// UI review round 3, display helpers (docs/ui-review/round3/):
//   UI3-03 person display label "Person: <appearance>" (fallback "Person <short id>"), never a crop image;
//   UI3-04 person events read "seen in X" / "left X" / "arrived in X" with no "by <actor>" clause, and one-sided moves
//          say "(destination not seen)" instead of "to an unknown location" (UI2-09 / UI-13 PARTIAL);
//   UI3-05 readable duplicate reason (no internal "live 'person' entities" text) and appearance-attribute chips;
//   UI3-07 crop detection and captions; UI3-11 history wording (split -> Unmerge, one "Pipeline update" label, pipeline
//          rows that only mirror a sighting are hidden).
import { describe, expect, test } from 'vitest';
import type { TimelineEntry } from '../src/api/types';
import { boxCaption, cropDisplayWidth, isCropUrl } from '../src/components/FrameImage';
import { eventPlace, eventSentence, isPersonEvent, personEventSentence } from '../src/lib/format';
import {
  appearanceChips, displayLabel, isPerson, personAppearance, personLabel, reviewReason, safePersonImage,
} from '../src/lib/person';
import { auditText, groupTimeline, hideMirroredAudits, timelineLabel } from '../src/pages/ItemDetailPage';

const PID = '87dbd193-0000-4000-8000-000000000001';

describe('UI3-03 person display label', () => {
  test('a person is "Person: <appearance>" from the description', () => {
    expect(personLabel({ entity_id: PID, label: 'person', description: 'black shirt, grey pants' })).toBe('Person: black shirt, grey pants');
  });

  test('the appearance drops a "person:" prefix, an internal reason and trailing punctuation', () => {
    expect(personAppearance("person: grey hoodie (15 live 'person' entities would exceed the expected count of 7)")).toBe('grey hoodie');
    expect(personAppearance('Short hair, blue shorts, orange top.')).toBe('short hair, blue shorts, orange top');
    expect(personAppearance('wearing a blue shirt and shorts')).toBe('blue shirt and shorts');
  });

  test('generic text is not an appearance: falls back to "Person <short id>"', () => {
    for (const d of [null, '', 'person', 'Crew member', 'crew-member', 'unknown person']) {
      expect(personLabel({ entity_id: PID, label: 'person', description: d })).toBe('Person 87dbd193');
    }
  });

  test('a descriptive label is used when there is no description', () => {
    expect(personLabel({ entity_id: PID, label: 'crew member, blue shirt', description: null })).toBe('Person: crew member, blue shirt');
  });

  test('long appearances are truncated to about 40 characters with an ellipsis', () => {
    const l = personLabel({ entity_id: PID, label: 'person', description: 'dark blue long-sleeve shirt, white pants, grey socks, headset' });
    expect(l.startsWith('Person: dark blue long-sleeve shirt')).toBe(true);
    expect(l.endsWith('…')).toBe(true);
    expect(l.length).toBeLessThanOrEqual('Person: '.length + 41);
  });

  test('isPerson: kind person or the crew-member catalog entry', () => {
    expect(isPerson({ kind: 'person' })).toBe(true);
    expect(isPerson({ catalog_id: 'crew-member' })).toBe(true);
    expect(isPerson({ kind: 'object', catalog_id: 'laptop' })).toBe(false);
    expect(isPerson(null)).toBe(false);
  });

  test('displayLabel: persons get the person label, objects keep their label', () => {
    expect(displayLabel({ entity_id: PID, kind: 'person', label: 'person', description: 'grey t-shirt' })).toBe('Person: grey t-shirt');
    expect(displayLabel({ entity_id: PID, kind: 'object', label: 'laptop', description: 'grey t-shirt' })).toBe('Laptop'); // Amended (UI5-10)
    expect(displayLabel({ entity_id: PID, label: '' })).toBe('item 87dbd193');
  });

  test('safePersonImage: crop / exemplar / face URLs are never used for a person', () => {
    expect(safePersonImage('/media/frames/2026/abc.jpg')).toBe('/media/frames/2026/abc.jpg');
    expect(safePersonImage('/media/crops/abc-5.jpg')).toBeNull();
    expect(safePersonImage('/api/media/exemplars/x.jpg')).toBeNull();
    expect(safePersonImage('/media/faces/x.jpg')).toBeNull();
    expect(safePersonImage(null)).toBeNull();
  });
});

describe('UI3-05 duplicate reason and appearance chips', () => {
  test('the internal expected-count text becomes a readable reason', () => {
    const r = reviewReason("person: grey hoodie (15 live 'person' entities would exceed the expected count of 7)");
    expect(r.reason).toBe('More person tracks than the expected crew size — these may be the same person');
    expect(r.text).toBe('person: grey hoodie');
    expect(r.text + r.reason).not.toMatch(/live|entities|expected count/);
  });

  test('the same pattern for an object reads as a duplicate hint with the expected count', () => {
    const r = reviewReason("laptop: grey laptop (4 live 'laptop' entities would exceed the expected count of 3)");
    expect(r.reason).toBe('More laptop items than the expected count (3) — some may be duplicates');
    expect(r.text).toBe('laptop: grey laptop');
  });

  test('other descriptions are unchanged, with no reason', () => {
    expect(reviewReason('camera: Black, handheld, with a lens.')).toEqual({ text: 'camera: Black, handheld, with a lens.', reason: null });
  });

  test('appearance chips pair colours with garments', () => {
    expect(appearanceChips('black shirt, grey pants')).toEqual(['black shirt', 'grey pants']);
    expect(appearanceChips('Short hair, blue shorts, orange top')).toEqual(['short hair', 'blue shorts', 'orange top']);
    expect(appearanceChips('dark blue shirt and white pants')).toEqual(['dark blue shirt', 'white pants']);
    expect(appearanceChips('spacesuit, dark visor')).toEqual(['spacesuit', 'dark visor']);
  });

  test('chips ignore words that are neither colours nor garments, and are capped', () => {
    expect(appearanceChips('person floating near the window')).toEqual([]);
    expect(appearanceChips('')).toEqual([]);
    expect(appearanceChips('red shirt, blue pants, green socks, white shoes, black hat, grey vest, tan belt').length).toBeLessThanOrEqual(6);
  });
});

describe('UI3-04 person events', () => {
  const cup = { to_location: 'CUP1', to_location_name: 'Cupola' };
  const nod1 = { from_location: 'NOD1', from_location_name: 'Node 1 Unity' };

  test('a person subject is detected by entity_catalog_id crew-member, entity_kind or the item kind', () => {
    expect(isPersonEvent({ entity_catalog_id: 'crew-member' })).toBe(true);
    expect(isPersonEvent({ entity_kind: 'person' })).toBe(true);
    expect(isPersonEvent({ entity_catalog_id: 'laptop' }, 'person')).toBe(true);
    expect(isPersonEvent({ entity_catalog_id: 'laptop' })).toBe(false);
  });

  test('"seen in", "left", "arrived in", with no actor', () => {
    expect(personEventSentence({ event_type: 'seen', ...cup, location_role: 'at' })).toBe('seen in Cupola (CUP1)');
    expect(personEventSentence({ event_type: 'moved', ...nod1, actor_desc: 'spacesuit' })).toBe('left Node 1 Unity (NOD1)');
    expect(personEventSentence({ event_type: 'moved', ...cup })).toBe('arrived in Cupola (CUP1)');
    expect(personEventSentence({ event_type: 'moved', ...nod1, ...cup })).toBe('moved from Node 1 Unity (NOD1) to Cupola (CUP1)');
    expect(personEventSentence({ event_type: 'added', ...cup })).toBe('seen in Cupola (CUP1)');
    expect(personEventSentence({ event_type: 'moved' })).toBe('seen (place not seen)'); // Amended (UI4-06): always a place
  });

  test('eventSentence with the person flag never says "by"', () => {
    const s = eventSentence({ event_type: 'moved', ...nod1, actor_desc: 'person (spacesuit, dark visor)', actor_kind: 'person' }, { person: true });
    expect(s).toBe('Left Node 1 Unity (NOD1)');
    expect(s).not.toMatch(/\bby\b/);
  });

  test('a one-sided move says "(destination not seen)", never "to an unknown location"', () => {
    expect(eventPlace({ event_type: 'moved', from_location: 'NOD3' })).toBe('from NOD3 (destination not seen)');
    expect(eventSentence({ event_type: 'moved', from_location: 'USOS', actor_desc: 'blue shirt' }))
      .toBe('Moved by person (blue shirt) from USOS (destination not seen)');
  });
});

describe('UI3-07 crops', () => {
  test('crop URLs are detected', () => {
    expect(isCropUrl('/media/crops/2348f197-5.jpg')).toBe(true);
    expect(isCropUrl('/api/media/crops/x.jpg')).toBe(true);
    expect(isCropUrl('/media/frames/x.jpg')).toBe(false);
    expect(isCropUrl(null)).toBe(false);
  });

  test('crop captions say "Close-up crop", never "Whole frame"', () => {
    expect(boxCaption(null, undefined, false, true)).toMatch(/^Close-up crop/);
    expect(boxCaption(null, undefined, true, true)).toMatch(/^Close-up crop/);
    expect(boxCaption(null, undefined, false, true)).not.toMatch(/whole frame/i);
  });

  test('a crop is never shown wider than 2x its natural width (nor wider than the space)', () => {
    expect(cropDisplayWidth(220, 1200)).toBe(440);
    expect(cropDisplayWidth(220, 300)).toBe(300);
    expect(cropDisplayWidth(0, 300)).toBe(300);
  });
});

describe('UI3-11 history wording', () => {
  const at = '2026-10-17T12:00:00Z';
  // Amended (UI4 / UI3-11 casing): sentence-case past tense, "Unmerged by" / "Merged by".
  test('the audit op "split" reads "Unmerged"', () => {
    expect(auditText('split by ui-review-3')).toBe('Unmerged by ui-review-3');
    expect(auditText('merge by ui-review-3')).toBe('Merged by ui-review-3');
  });

  test('pipeline updates use one label: "Pipeline update" / "N pipeline updates"', () => {
    expect(auditText('update by pipeline')).toBe('Pipeline update');
    const e: TimelineEntry = { at, kind: 'audit', summary: 'update by pipeline' };
    expect(timelineLabel(groupTimeline([e, { ...e, at: '2026-10-17T11:59:00Z' }])[0])).toBe('2 pipeline updates');
    expect(timelineLabel(groupTimeline([e])[0])).toBe('Pipeline update');
    const c: TimelineEntry = { at, kind: 'audit', summary: '9 pipeline updates (sighting refresh: last_seen_at/confidence)', count: 9 };
    expect(timelineLabel(groupTimeline([c])[0])).toBe('9 pipeline updates (sighting refresh: last_seen_at/confidence)');
  });

  test('a single pipeline update at the same time as a sighting is hidden; others stay', () => {
    const entries: TimelineEntry[] = [
      { at: '2026-10-17T12:00:01Z', kind: 'audit', summary: 'update by pipeline' },
      { at: '2026-10-17T12:00:00Z', kind: 'sighting', summary: 'seen', image_url: '/media/frames/a.jpg' },
      { at: '2026-10-17T11:00:00Z', kind: 'audit', summary: 'update by pipeline' },
      { at: '2026-10-17T10:00:00Z', kind: 'audit', summary: 'update by ui-review-3' },
      { at: '2026-10-17T10:00:00Z', kind: 'sighting', summary: 'seen' },
    ];
    const out = hideMirroredAudits(entries);
    expect(out.map((e) => `${e.kind} ${e.at.slice(11, 19)}`)).toEqual([
      'sighting 12:00:00', 'audit 11:00:00', 'audit 10:00:00', 'sighting 10:00:00',
    ]);
  });
});
