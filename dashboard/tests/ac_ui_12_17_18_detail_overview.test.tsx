// UI review fixes on the item detail and overview:
//   AC-UI-12 Activity list (all event types, natural phrasing, no "?"), AC-UI-17 history grouping + kind filter chips,
//   AC-UI-18 overview feed with labels and location names.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { Event, TimelineEntry } from '../src/api/types';
import { groupTimeline } from '../src/pages/ItemDetailPage';
import { eventRoute } from '../src/pages/OverviewPage';
import { ID, RECENT_EVENTS, drillDetail, drillHistory } from './fixtures';
import { server } from './server';
import { renderApp } from './render';

const detailPath = `/items/${ID.drill}`;
async function openDetail() {
  const r = renderApp(detailPath);
  await screen.findByRole('heading', { name: /pistol-grip drill/i });
  return r;
}

const usedEvent: Event = {
  event_id: 'e0000000-0000-4000-8000-0000000000u1', event_type: 'used', entity_id: ID.drill,
  actor_desc: 'Blue shirt, black shorts, headset', from_location: null, to_location: null,
  occurred_at: '2026-10-17T11:30:00Z', confidence: 0.7, evidence_url: '/media/ev/used.jpg', source: 'pipeline',
};
const oneSidedMove: Event = {
  event_id: 'e0000000-0000-4000-8000-0000000000u2', event_type: 'moved', entity_id: ID.drill,
  actor_desc: 'wearing a blue tank top', from_location: 'LAB1S2', to_location: null,
  occurred_at: '2026-10-17T11:45:00Z', confidence: 0.7, evidence_url: null, source: 'pipeline',
  from_location_name: 'Lab starboard rack 2',
};

describe('AC-UI-12 item detail Activity', () => {
  test('AC-UI-12 Activity lists used and handed-over events too, newest first, with natural phrasing', async () => {
    server.use(
      http.get(`*/api/items/${ID.drill}`, () =>
        HttpResponse.json({ ...drillDetail, events: [...(drillDetail.events ?? []), usedEvent, oneSidedMove] }),
      ),
    );
    await openDetail();
    const list = screen.getByRole('list', { name: /activity/i });
    const lines = within(list).getAllByRole('listitem').map((li) => li.textContent ?? '');
    expect(lines).toHaveLength(4);
    // Amended (UI2-09): "<Verb> by <actor> <place> · <UTC>"; a use with no location has no place.
    // Amended (UI3-04): a move with no destination says "(destination not seen)".
    expect(lines[0]).toMatch(/^Moved by person wearing a blue tank top from Lab starboard rack 2 \(LAB1S2\) \(destination not seen\) · 2026-10-17 11:45:00 UTC/);
    expect(lines[1]).toMatch(/^Used by person \(blue shirt, black shorts, headset\) · 2026-10-17 11:30:00 UTC/);
    expect(lines.join('\n')).not.toMatch(/\?|No recorded/);
  });

  test('AC-UI-12 activity evidence frames are shown and can be enlarged', async () => {
    server.use(http.get(`*/api/items/${ID.drill}`, () => HttpResponse.json({ ...drillDetail, events: [usedEvent] })));
    const { user } = await openDetail();
    const list = screen.getByRole('list', { name: /activity/i });
    expect(within(list).getByRole('img')).toHaveAttribute('src', '/api/media/ev/used.jpg');
    await user.click(within(list).getByRole('button', { name: /enlarge image/i }));
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  test('AC-UI-12 no events -> an explicit empty line', async () => {
    server.use(http.get(`*/api/items/${ID.drill}`, () => HttpResponse.json({ ...drillDetail, events: [] })));
    await openDetail();
    expect(screen.getByText(/no recorded activity/i)).toBeInTheDocument();
  });
});

const pipeline = (at: string): TimelineEntry => ({ at, kind: 'audit', summary: 'update by pipeline', image_url: null });

describe('AC-UI-17 history grouping', () => {
  test('AC-UI-17 groupTimeline folds consecutive identical audit rows and honours API counts', () => {
    const g = groupTimeline([
      pipeline('2026-10-17T12:04:00Z'),
      pipeline('2026-10-17T12:03:00Z'),
      { ...pipeline('2026-10-17T12:02:00Z'), count: 5, first_at: '2026-10-17T11:00:00Z' },
      drillHistory[0],
      pipeline('2026-10-17T10:00:00Z'),
    ]);
    expect(g.map((x) => x.count)).toEqual([7, 1, 1]);
    expect(g[0].firstAt).toBe('2026-10-17T11:00:00Z');
    expect(g[0].members).toHaveLength(3);
  });

  test('AC-UI-17 14 repeated "update by pipeline" rows render as one "14 × update by pipeline" row', async () => {
    const audits = Array.from({ length: 14 }, (_, i) => pipeline(`2026-10-17T12:${String(30 - i).padStart(2, '0')}:00Z`));
    server.use(http.get(`*/api/items/${ID.drill}/history`, () => HttpResponse.json([...audits, ...drillHistory])));
    const { user } = await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    const rows = within(list).getAllByRole('listitem');
    expect(rows).toHaveLength(4);
    // Amended (UI3-11): one "Pipeline update" label; folded runs read "N pipeline updates".
    expect(rows[0]).toHaveTextContent('14 pipeline updates');
    await user.click(within(rows[0]).getByRole('button', { name: /show 14 entries/i }));
    expect(within(rows[0]).getByRole('list', { name: /folded entries/i }).children).toHaveLength(14);
  });

  test('AC-UI-17 an API-collapsed entry (count) shows its count without a disclosure', async () => {
    server.use(
      http.get(`*/api/items/${ID.drill}/history`, () =>
        HttpResponse.json([{ ...pipeline('2026-10-17T12:30:00Z'), count: 9, first_at: '2026-10-17T12:00:00Z' }, ...drillHistory]),
      ),
    );
    await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    // Amended (UI3-11): one "Pipeline update" label.
    expect(within(list).getByText('9 pipeline updates')).toBeInTheDocument();
    expect(within(list).getByText(/back to 2026-10-17 12:00:00 UTC/)).toBeInTheDocument();
  });

  test('AC-UI-17 kind chips filter the timeline (audit hidden)', async () => {
    const { user } = await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    expect(within(list).getAllByRole('listitem')).toHaveLength(3);
    const chips = screen.getByRole('group', { name: /show history entries/i });
    const audit = within(chips).getByRole('button', { name: /audit \(1\)/i });
    expect(audit).toHaveAttribute('aria-pressed', 'true');
    await user.click(audit);
    expect(audit).toHaveAttribute('aria-pressed', 'false');
    await waitFor(() => expect(within(screen.getByRole('list', { name: /^history$/i })).getAllByRole('listitem')).toHaveLength(2));
    expect(screen.queryByText(/Label edited by alice/)).toBeNull();
  });

  test('AC-UI-17 #history deep link: the History section has id="history"', async () => {
    renderApp(`${detailPath}#history`);
    await screen.findByRole('heading', { name: /pistol-grip drill/i });
    expect(document.getElementById('history')).toHaveAccessibleName(/history/i);
  });
});

describe('AC-UI-18 overview feed shows labels and location names', () => {
  const labelled: Event[] = [
    {
      ...RECENT_EVENTS[0], entity_label: 'laptop', entity_catalog_id: 'laptop-ssc', actor_desc: 'sleeping, wearing a headset',
      from_location_name: 'Node 2 deck bay 4', to_location_name: 'Lab starboard rack 2',
    },
    { ...RECENT_EVENTS[1], entity_label: 'Half CTB', to_location_name: 'Node 2 overhead bay 4' },
    { ...RECENT_EVENTS[0], event_id: 'e0000000-0000-4000-8000-0000000000b3', from_location: 'NOD3', to_location: null },
    { ...RECENT_EVENTS[0], event_id: 'e0000000-0000-4000-8000-0000000000b4', from_location: null, to_location: null, event_type: 'used' },
  ];

  test('AC-UI-18 events read "MOVED laptop by person (...) Name (CODE) → Name (CODE)" with UTC + relative time', async () => {
    server.use(http.get('*/api/events', () => HttpResponse.json(labelled)));
    renderApp('/overview');
    const feed = await screen.findByRole('list', { name: /recent events/i });
    const items = await within(feed).findAllByRole('listitem');
    expect(within(items[0]).getByRole('link', { name: /laptop/ })).toHaveAttribute('href', `/items/${ID.drill}`);
    expect(items[0]).toHaveTextContent('by person (sleeping, wearing a headset)');
    // Amended (UI2-09): "from A to B" instead of "A → B".
    expect(items[0]).toHaveTextContent('from Node 2 deck bay 4 (NOD2D4) to Lab starboard rack 2 (LAB1S2)');
    expect(items[0]).toHaveTextContent('2026-10-17 11:00 UTC');
    expect(items[1]).toHaveTextContent('at Node 2 overhead bay 4 (NOD2O4)');
  });

  test('AC-UI-18 fallbacks: short id when no label, code when no name, one-sided "from X", no route when both missing', async () => {
    server.use(http.get('*/api/events', () => HttpResponse.json(labelled)));
    renderApp('/overview');
    const feed = await screen.findByRole('list', { name: /recent events/i });
    const items = await within(feed).findAllByRole('listitem');
    expect(within(items[2]).getByRole('link', { name: `item ${ID.drill.slice(0, 8)}` })).toBeInTheDocument();
    expect(items[2]).toHaveTextContent('from NOD3');
    expect(items[2].textContent).not.toMatch(/—|→/);
    expect(items[3].textContent).not.toMatch(/→|from |to /);
  });

  test('AC-UI-18 eventRoute unit cases', () => {
    expect(eventRoute({ from_location: 'A', to_location: 'B' })).toBe('from A to B'); // amended (UI2-09)
    expect(eventRoute({ event_type: 'added', to_location: 'NOD1', to_location_name: 'Node 1' })).toBe('at Node 1 (NOD1)');
    expect(eventRoute({ event_type: 'moved', to_location: 'NOD1' })).toBe('to NOD1');
    expect(eventRoute({})).toBe('');
  });
});
