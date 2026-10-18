// UI review round 2, unit level:
//   UI2-09 / UI-04 / UI-13 event wording ("used at X", "moved from A to B", never "-> ?" or "to an unknown location"
//   for non-moves); UI2-10 operators are "operator <id>" (actor_kind, falling back to source); UI2-11 history label
//   "9 pipeline updates" (no "9 × 9"); UI2-12 Markdown numbered lists continue across nested bullets and keep "3.".
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, test } from 'vitest';
import type { TimelineEntry } from '../src/api/types';
import { eventActor, eventPlace, eventSentence, legacyEventSummary } from '../src/lib/format';
import { renderMarkdown } from '../src/lib/markdown';
import { groupTimeline, timelineLabel } from '../src/pages/ItemDetailPage';

describe('UI2-09 eventPlace / eventSentence', () => {
  test('a use with one location reads "at X" (never "to an unknown location")', () => {
    const e = { event_type: 'used', from_location: 'USOS', from_location_name: 'US Orbital Segment', actor_desc: 'Blue shirt' };
    expect(eventPlace(e)).toBe('at US Orbital Segment (USOS)');
    expect(eventSentence(e)).toBe('Used by person (blue shirt) at US Orbital Segment (USOS)');
  });

  test('a use with only to_location also reads "at X"; added reads "at X"', () => {
    expect(eventPlace({ event_type: 'used', to_location: 'NOD1', to_location_name: 'Node 1' })).toBe('at Node 1 (NOD1)');
    expect(eventPlace({ event_type: 'added', to_location: 'NOD3' })).toBe('at NOD3');
    expect(eventPlace({ event_type: 'handed_over', from_location: 'LAB1', to_location: 'LAB1' })).toBe('at LAB1');
  });

  // Amended (UI3-04, UI2-09 / UI-13 PARTIAL): a one-sided move says "(destination not seen)".
  test('a move reads "from A to B"; a one-sided move says "(destination not seen)"', () => {
    expect(eventPlace({ event_type: 'moved', from_location: 'NOD3', from_location_name: 'Node 3', to_location: 'NOD1', to_location_name: 'Node 1' }))
      .toBe('from Node 3 (NOD3) to Node 1 (NOD1)');
    expect(eventPlace({ event_type: 'moved', from_location: 'NOD3' })).toBe('from NOD3 (destination not seen)');
    expect(eventPlace({ event_type: 'moved', to_location: 'NOD1' })).toBe('to NOD1');
  });

  test('location_role from the API wins over the type heuristic', () => {
    expect(eventPlace({ event_type: 'moved', from_location: 'NOD3', to_location: 'NOD1', location_role: 'at' })).toBe('at NOD1');
    expect(eventPlace({ event_type: 'used', from_location: 'NOD3', to_location: 'NOD1', location_role: 'from_to' })).toBe('from NOD3 to NOD1');
    expect(eventPlace({ event_type: 'used', from_location: 'NOD3', location_role: 'from_to' })).toBe('from NOD3');
  });

  test('no location: no place, and the sentence is just verb + actor', () => {
    expect(eventPlace({ event_type: 'used' })).toBe('');
    expect(eventSentence({ event_type: 'used', actor_desc: null })).toBe('Used by an unknown person');
  });

  test('no sentence ever contains "?" or "->"', () => {
    for (const t of ['used', 'moved', 'added', 'handed_over', 'stowed']) {
      for (const [from, to] of [['A', null], [null, 'B'], [null, null], ['A', 'B']]) {
        const s = eventSentence({ event_type: t, from_location: from, to_location: to, actor_desc: 'x' });
        expect(s).not.toMatch(/\?|->/);
        if (t !== 'moved') expect(s).not.toMatch(/unknown location/);
      }
    }
  });
});

describe('UI2-10 eventActor: operators are not people', () => {
  test('actor_kind operator -> "operator <id>"', () => {
    expect(eventActor({ actor_desc: 'ui-review-2', actor_kind: 'operator' })).toBe('operator ui-review-2');
    expect(eventActor({ actor_desc: 'Operator dave', actor_kind: 'operator' })).toBe('operator dave');
    expect(eventActor({ actor_desc: null, actor_kind: 'operator' })).toBe('an operator');
  });

  test('actor_kind person -> person phrasing even when source is operator', () => {
    expect(eventActor({ actor_desc: 'Blue shirt', actor_kind: 'person', source: 'operator' })).toBe('person (blue shirt)');
  });

  test('without actor_kind, source=operator (or api) means an operator', () => {
    expect(eventActor({ actor_desc: 'ui-review-2', source: 'operator' })).toBe('operator ui-review-2');
    expect(eventActor({ actor_desc: 'ui-review-2', source: 'api' })).toBe('operator ui-review-2');
    expect(eventActor({ actor_desc: 'operator', source: 'operator' })).toBe('operator');
    expect(eventActor({ actor_desc: null, source: 'operator' })).toBe('an operator');
    expect(eventActor({ actor_desc: 'Blue shirt', source: 'pipeline' })).toBe('person (blue shirt)');
    expect(eventActor({ actor_desc: 'Blue shirt' })).toBe('person (blue shirt)');
  });

  test('sentence uses the operator phrasing', () => {
    expect(eventSentence({ event_type: 'moved', actor_desc: 'ui-review-2', actor_kind: 'operator', from_location: 'NOD3', to_location: 'NOD1' }))
      .toBe('Moved by operator ui-review-2 from NOD3 to NOD1');
  });
});

describe('UI-04 legacy history event summaries are rewritten', () => {
  test('"used USOS -> ? by Blue shirt" -> "Used by person (blue shirt) at USOS"', () => {
    expect(legacyEventSummary('used USOS -> ? by Blue shirt, black shorts, headset'))
      .toBe('Used by person (blue shirt, black shorts, headset) at USOS');
  });

  test('moves, missing actor and operator source', () => {
    expect(legacyEventSummary('moved NOD3 -> NOD1 by ui-review-2', { source: 'operator' })).toBe('Moved by operator ui-review-2 from NOD3 to NOD1');
    // Amended (UI5-09): a pipeline move with no origin is the first sighting.
    expect(legacyEventSummary('moved ? -> NOD1')).toBe('First seen in NOD1');
    expect(legacyEventSummary('used')).toBe('Used by an unknown person');
  });

  test('summaries in another shape are left alone (null)', () => {
    expect(legacyEventSummary('Moved from NOD2D4 to LAB1S2')).toBeNull();
    expect(legacyEventSummary('')).toBeNull();
  });
});

describe('UI2-11 history label has no double count', () => {
  const at = '2026-10-17T12:00:00Z';
  test('an API-collapsed entry whose summary starts with its count reads "9 pipeline updates…"', () => {
    const e: TimelineEntry = { at, kind: 'audit', summary: '9 pipeline updates (sighting refresh: last_seen_at/confidence)', count: 9 };
    const [g] = groupTimeline([e]);
    expect(timelineLabel(g)).toBe('9 pipeline updates (sighting refresh: last_seen_at/confidence)');
  });

  test('a client-folded run keeps "N × summary"', () => {
    const e: TimelineEntry = { at, kind: 'audit', summary: 'update by pipeline' };
    const [g] = groupTimeline([e, { ...e, at: '2026-10-17T11:00:00Z' }]);
    // Amended (UI3-11): pipeline updates use one label ("N pipeline updates"); other folded summaries keep "N ×".
    expect(timelineLabel(g)).toBe('2 pipeline updates');
    const o: TimelineEntry = { at, kind: 'audit', summary: 'update by ui-review-3' };
    expect(timelineLabel(groupTimeline([o, { ...o, at: '2026-10-17T11:00:00Z' }])[0])).toBe('2 × Updated by ui-review-3'); // Amended (UI4 / UI3-11): sentence case
  });

  test('two adjacent server-collapsed runs add up and the leading number is the total', () => {
    const e: TimelineEntry = { at, kind: 'audit', summary: '3 pipeline updates (x)', count: 3 };
    const f: TimelineEntry = { ...e, at: '2026-10-17T11:00:00Z', summary: '3 pipeline updates (x)', count: 3 };
    const [g] = groupTimeline([e, f]);
    expect(timelineLabel(g)).toBe('6 pipeline updates (x)');
  });

  test('single entries are unchanged', () => {
    const [g] = groupTimeline([{ at, kind: 'audit', summary: 'edit by alice' }]);
    expect(timelineLabel(g)).toBe('Edit by alice'); // Amended (UI4 / UI3-11): sentence case
  });
});

function md(source: string) {
  return render(<MemoryRouter><div data-testid="md">{renderMarkdown(source)}</div></MemoryRouter>).getByTestId('md');
}

describe('UI2-12 Markdown ordered lists', () => {
  test('nested bullets stay inside the numbered item; numbering continues (one <ol>)', () => {
    const el = md('1. **Laptop** a\n   - Location: USOS\n   - Seen: 22:51\n2. **Laptop** b\n   - Location: NOD1\n3. **Laptop** c');
    const ols = el.querySelectorAll('ol');
    expect(ols).toHaveLength(1);
    const top = Array.from(ols[0].children);
    expect(top).toHaveLength(3);
    expect(top[0].querySelectorAll('ul > li')).toHaveLength(2);
    expect(top[0]).toHaveTextContent(/Location: USOS/);
    expect(top[1]).toHaveTextContent(/Laptop b/);
  });

  test('a list that starts at 3 keeps "3." (start attribute)', () => {
    const el = md('3. third\n4. fourth');
    expect(el.querySelector('ol')).toHaveAttribute('start', '3');
  });

  test('a list split by a paragraph resumes at the number in the text', () => {
    const el = md('1. one\n\nA note.\n\n2. two');
    const ols = el.querySelectorAll('ol');
    expect(ols).toHaveLength(2);
    expect(ols[0].getAttribute('start')).toBeNull();
    expect(ols[1]).toHaveAttribute('start', '2');
  });

  test('_em_ is italic; snake_case identifiers stay literal', () => {
    const el = md('this is _important_ but last_seen_at and first_seen_at are not');
    expect(el.querySelectorAll('em')).toHaveLength(1);
    expect(el.querySelector('em')).toHaveTextContent('important');
    expect(el).toHaveTextContent('last_seen_at and first_seen_at');
  });

  test('nested-list content stays text (no injected markup)', () => {
    const el = md('1. a\n   - <img src=x onerror=alert(1)>');
    expect(el.querySelector('img')).toBeNull();
    expect(el).toHaveTextContent('<img src=x onerror=alert(1)>');
  });
});
