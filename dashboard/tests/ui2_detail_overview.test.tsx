// UI review round 2 on the item detail, overview and inventory:
//   UI2-05 merged banner + Unmerge (operator required, 409 handled) and "Merged from" on the target;
//   UI2-07 / UI-08 box on the hero image (detector solid, omni dashed "approximate", none = "whole frame");
//   UI2-09 / UI-04 / UI-13 / UI2-10 event wording in Activity, History rows, the overview feed and Last handled;
//   UI2-11 "9 pipeline updates" (no "9 × 9"). Every new API field is optional: both cases are tested.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { Event, ItemDetail, TimelineEntry } from '../src/api/types';
import { ID, RECENT_EVENTS, drill, drillDetail, drillHistory } from './fixtures';
import { requestsTo, server } from './server';
import { current, findItem, findToast, renderApp } from './render';

const TARGET = ID.bag;
const SOURCE = ID.drill;
const detailPath = `/items/${SOURCE}`;
const unmergePath = `/api/items/${SOURCE}/unmerge`;

const mergedSource: ItemDetail = {
  ...drillDetail,
  status: 'merged',
  merged_into: { entity_id: TARGET, label: 'cargo transfer bag' },
  merged_at: '2026-10-18T00:07:10Z',
  sightings: [],
};

function detailIs(item: ItemDetail, id = SOURCE) {
  server.use(http.get(`*/api/items/${id}`, () => HttpResponse.json(item)));
}

async function openDetail(path = detailPath, heading: RegExp = /pistol-grip drill/i) {
  const r = renderApp(path);
  await screen.findByRole('heading', { name: heading, level: 1 });
  return r;
}

describe('UI2-05 merged item banner and Unmerge', () => {
  test('a merged item says where it went, with a link and the UTC time', async () => {
    detailIs(mergedSource);
    await openDetail();
    const banner = screen.getByRole('region', { name: /merged/i });
    // Amended (UI3-11): the banner names the target with its short id.
    expect(banner).toHaveTextContent(/Merged into cargo transfer bag 44444444 at 2026-10-18 00:07 UTC/);
    expect(within(banner).getByRole('link', { name: /cargo transfer bag/i })).toHaveAttribute('href', `/items/${TARGET}`);
  });

  test('status merged without merged_into (older API) still shows a banner, without a link', async () => {
    detailIs({ ...drillDetail, status: 'merged' });
    await openDetail();
    const banner = screen.getByRole('region', { name: /merged/i });
    expect(banner).toHaveTextContent(/merged into another item/i);
    expect(within(banner).queryByRole('link')).toBeNull();
  });

  test('a live item has no merged banner', async () => {
    await openDetail();
    expect(screen.queryByRole('region', { name: /merged/i })).toBeNull();
  });

  test('Unmerge needs an operator name, then posts {operator} and reloads the item', async () => {
    let unmerged = false;
    server.use(
      http.get(`*/api/items/${SOURCE}`, () => HttpResponse.json(unmerged ? drillDetail : mergedSource)),
      http.post(`*/api/items/${SOURCE}/unmerge`, () => {
        unmerged = true;
        return HttpResponse.json([drill, { ...drill, entity_id: TARGET, label: 'cargo transfer bag' }]);
      }),
    );
    const { user } = await openDetail();
    const banner = screen.getByRole('region', { name: /merged/i });
    const button = within(banner).getByRole('button', { name: /^unmerge/i });
    expect(button).toBeDisabled();
    await user.type(within(banner).getByLabelText(/^operator$/i), 'ui-review-2');
    expect(button).toBeEnabled();
    await user.click(button);
    await waitFor(() => expect(requestsTo('POST', unmergePath)).toHaveLength(1));
    expect(requestsTo('POST', unmergePath)[0].body).toMatchObject({ operator: 'ui-review-2' });
    expect(await findToast(/unmerged/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('region', { name: /merged/i })).toBeNull());
  });

  test('a 409 from unmerge is explained in plain language and the banner stays', async () => {
    window.localStorage.setItem('nasa-omni.operator', 'ui-review-2');
    detailIs(mergedSource);
    server.use(
      http.post(`*/api/items/${SOURCE}/unmerge`, () =>
        HttpResponse.json({ detail: 'target has new sightings since the merge' }, { status: 409 })),
    );
    const { user } = await openDetail();
    const banner = screen.getByRole('region', { name: /merged/i });
    await user.click(within(banner).getByRole('button', { name: /^unmerge/i }));
    expect(await within(banner).findByRole('alert')).toHaveTextContent(
      /can't be undone safely.*target has new sightings since the merge/i,
    );
    expect(screen.getByRole('region', { name: /merged/i })).toBeInTheDocument();
  });

  test('the target lists "Merged from" with links and times', async () => {
    detailIs({
      ...drillDetail,
      merged_from: [{ entity_id: ID.camera, label: 'DSLR camera', merged_at: '2026-10-18T00:07:10Z' }],
    });
    await openDetail();
    const list = screen.getByRole('list', { name: /merged from/i });
    expect(within(list).getByRole('link', { name: /DSLR camera/ })).toHaveAttribute('href', `/items/${ID.camera}`);
    expect(list).toHaveTextContent('2026-10-18 00:07 UTC');
  });

  test('no merged_from -> no "Merged from" list', async () => {
    await openDetail();
    expect(screen.queryByRole('list', { name: /merged from/i })).toBeNull();
  });
});

describe('UI2-07 / UI-08 hero image box', () => {
  test('a detector box on the snapshot is drawn solid, and the caption says so', async () => {
    detailIs({ ...drillDetail, snapshot_bbox: [0.1, 0.2, 0.5, 0.6], snapshot_bbox_source: 'detector' });
    await openDetail();
    const media = screen.getByTestId('detail-media');
    const box = within(media).getByTestId('bbox');
    expect(box).toHaveClass('bbox-detector');
    expect(box).not.toHaveClass('bbox-approx');
    expect(box.style.left).toBe('10%');
    expect(media).toHaveTextContent(/outlined box marks the object/i);
  });

  test('an omni box is dashed, labelled "approximate" and padded', async () => {
    detailIs({ ...drillDetail, snapshot_bbox: [0.4, 0.4, 0.6, 0.6], snapshot_bbox_source: 'omni' });
    await openDetail();
    const media = screen.getByTestId('detail-media');
    const box = within(media).getByTestId('bbox');
    expect(box).toHaveClass('bbox-approx');
    // The on-image "approximate" tag is CSS generated content (.bbox-approx::after, see ui2_layout_css), so it does not
    // add visible text inside the enlarge button (axe label-content-name-mismatch); the caption says it in text.
    expect(box.style.left).toBe('37%');
    expect(box.style.width).toBe('26%');
    expect(media).toHaveTextContent(/approximate/i);
  });

  test('no snapshot box -> no overlay and the caption says "whole frame"', async () => {
    await openDetail();
    const media = screen.getByTestId('detail-media');
    expect(within(media).queryByTestId('bbox')).toBeNull();
    expect(media).toHaveTextContent(/whole frame/i);
  });

  test('history sighting rows draw the sighting bbox when the detail carries it', async () => {
    const hist: TimelineEntry[] = [{ ...drillHistory[0], id: 's1' }];
    server.use(
      http.get(`*/api/items/${SOURCE}/history`, () => HttpResponse.json(hist)),
      http.get(`*/api/items/${SOURCE}`, () =>
        HttpResponse.json({ ...drillDetail, sightings: [{ sighting_id: 's1', bbox: [0.2, 0.2, 0.4, 0.4], bbox_source: 'detector' }] })),
    );
    await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    const box = await within(list).findByTestId('bbox');
    expect(box).toHaveClass('bbox-detector');
  });
});

const usedEvent: Event = {
  event_id: 'e0000000-0000-4000-8000-0000000000f1', event_type: 'used', entity_id: SOURCE,
  actor_desc: 'Blue shirt, black shorts, headset', from_location: 'USOS', to_location: null,
  from_location_name: 'US Orbital Segment', occurred_at: '2026-10-17T22:50:05Z', confidence: 0.7,
  evidence_url: null, source: 'pipeline',
};
const operatorMove: Event = {
  event_id: 'e0000000-0000-4000-8000-0000000000f2', event_type: 'moved', entity_id: SOURCE,
  actor_desc: 'ui-review-2', from_location: 'NOD3', to_location: 'NOD1', from_location_name: 'Node 3',
  to_location_name: 'Node 1', occurred_at: '2026-10-18T00:06:31Z', confidence: 1, evidence_url: null,
  source: 'operator', actor_kind: 'operator', location_role: 'from_to',
};

describe('UI2-09 / UI2-10 / UI-04 activity and history wording', () => {
  test('Activity: "Used by … at US Orbital Segment (USOS)" and "Moved by operator ui-review-2 from Node 3 (NOD3) to Node 1 (NOD1)"', async () => {
    detailIs({ ...drillDetail, events: [usedEvent, operatorMove] });
    await openDetail();
    const lines = within(screen.getByRole('list', { name: /activity/i })).getAllByRole('listitem').map((l) => l.textContent ?? '');
    expect(lines[0]).toMatch(/^Moved by operator ui-review-2 from Node 3 \(NOD3\) to Node 1 \(NOD1\)/);
    expect(lines[0]).toMatch(/2026-10-18 00:06:31 UTC/);
    expect(lines[1]).toMatch(/^Used by person \(blue shirt, black shorts, headset\) at US Orbital Segment \(USOS\)/);
    expect(lines.join('\n')).not.toMatch(/unknown location|person \(ui-review-2\)/);
  });

  test('History EVENT rows use the same sentence (matched by event id), never "-> ?"', async () => {
    detailIs({ ...drillDetail, events: [usedEvent] });
    server.use(
      http.get(`*/api/items/${SOURCE}/history`, () =>
        HttpResponse.json([{ at: usedEvent.occurred_at, kind: 'event', id: usedEvent.event_id, event_type: 'used', source: 'pipeline', location_id: 'USOS', summary: 'used USOS -> ? by Blue shirt, black shorts, headset', image_url: null }])),
    );
    await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    await waitFor(() => expect(list).toHaveTextContent('Used by person (blue shirt, black shorts, headset) at US Orbital Segment (USOS)'));
    expect(list.textContent).not.toMatch(/->|\?/);
  });

  test('History EVENT rows without a matching event are rewritten from the summary', async () => {
    server.use(
      http.get(`*/api/items/${SOURCE}/history`, () =>
        HttpResponse.json([{ at: '2026-10-18T00:06:31Z', kind: 'event', source: 'operator', location_id: 'NOD1', summary: 'moved NOD3 -> NOD1 by ui-review-2', image_url: null }])),
    );
    await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    expect(list).toHaveTextContent('Moved by operator ui-review-2 from NOD3 to NOD1');
  });

  test('UI2-11 a server-collapsed audit row reads "9 pipeline updates (…)", not "9 × 9"', async () => {
    server.use(
      http.get(`*/api/items/${SOURCE}/history`, () =>
        HttpResponse.json([{ at: '2026-10-17T12:30:00Z', kind: 'audit', summary: '9 pipeline updates (sighting refresh: last_seen_at/confidence)', count: 9, first_at: '2026-10-17T12:00:00Z', image_url: null }])),
    );
    await openDetail();
    const list = await screen.findByRole('list', { name: /^history$/i });
    expect(list).toHaveTextContent('9 pipeline updates (sighting refresh: last_seen_at/confidence)');
    expect(list.textContent).not.toMatch(/9 × 9/);
  });

  test('UI2-10 Last handled shows an operator edit as "Moved by operator …" (actor_kind on last_handled)', async () => {
    detailIs({ ...drillDetail, last_handled: { event_type: 'moved', actor_desc: 'ui-review-2', actor_kind: 'operator', occurred_at: '2026-10-18T00:06:31Z' } });
    await openDetail();
    expect(screen.getAllByText('Moved by operator ui-review-2').length).toBeGreaterThan(0);
  });

  test('UI2-10 without actor_kind, an actor equal to location_set_by is an operator', async () => {
    server.use(
      http.get('*/api/items', () =>
        HttpResponse.json({
          items: [{ ...drill, location_set_by: 'ui-review-2', last_handled: { event_type: 'moved', actor_desc: 'ui-review-2', occurred_at: '2026-10-18T00:06:31Z' } }],
          total: 1,
        })),
    );
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(within(row).getByText('Moved by operator ui-review-2')).toBeInTheDocument();
    expect(row.textContent).not.toMatch(/person \(ui-review-2\)/);
  });
});

describe('UI2-09 / UI-13 / UI2-10 overview feed wording', () => {
  const feed: Event[] = [
    { ...RECENT_EVENTS[1], entity_label: 'cargo transfer bag', to_location: 'NOD3', to_location_name: 'Node 3 Tranquility', from_location: null },
    { ...usedEvent, entity_label: 'camera' },
    { ...operatorMove, entity_label: 'laptop' },
    { ...operatorMove, event_id: 'e0000000-0000-4000-8000-0000000000f3', actor_kind: undefined, location_role: undefined, entity_label: 'drill' },
    { ...usedEvent, event_id: 'e0000000-0000-4000-8000-0000000000f4', from_location: null, to_location: 'NOD1', to_location_name: 'Node 1 Unity', entity_label: 'added-to' , event_type: 'added', source: 'pipeline' },
  ];

  test('added/used read "at X", moves "from A to B", operators "by operator <id>"', async () => {
    server.use(http.get('*/api/events', () => HttpResponse.json(feed)));
    renderApp('/overview');
    const items = await within(await screen.findByRole('list', { name: /recent events/i })).findAllByRole('listitem');
    expect(items[0]).toHaveTextContent('at Node 3 Tranquility (NOD3)');
    expect(items[0]).toHaveTextContent(/by an operator|by operator/);
    expect(items[0].textContent).not.toMatch(/from Node 3/);
    expect(items[1]).toHaveTextContent('at US Orbital Segment (USOS)');
    expect(items[1].textContent).not.toMatch(/from US Orbital/);
    expect(items[2]).toHaveTextContent('by operator ui-review-2');
    expect(items[2]).toHaveTextContent('from Node 3 (NOD3) to Node 1 (NOD1)');
    expect(items[3]).toHaveTextContent('by operator ui-review-2'); // source fallback
    expect(items[4]).toHaveTextContent('at Node 1 Unity (NOD1)');
    for (const li of items) expect(li.textContent).not.toMatch(/person \(ui-review|\?|unknown location/);
  });
});

test('UI2-05 the merged banner link opens the target', async () => {
  detailIs(mergedSource);
  const { user } = await openDetail();
  await user.click(within(screen.getByRole('region', { name: /merged/i })).getByRole('link', { name: /cargo transfer bag/i }));
  await waitFor(() => expect(current.pathname).toBe(`/items/${TARGET}`));
});

describe('UI2-07 a11y: the "approximate" tag is part of the enlarge button name (axe label-content-name-mismatch)', () => {
  test('omni box -> button name ends with "(approximate box)"; detector/unknown -> unchanged', async () => {
    detailIs({ ...drillDetail, snapshot_bbox: [0.4, 0.4, 0.6, 0.6], snapshot_bbox_source: 'omni' });
    await openDetail();
    const media = screen.getByTestId('detail-media');
    expect(within(media).getByRole('button', { name: /enlarge image/i })).toHaveAccessibleName(/\(approximate box\)$/);
  });

  test('links inside sentences are underlined (axe link-in-text-block)', async () => {
    const { readFileSync } = await import('node:fs');
    const { resolve } = await import('node:path');
    const css = readFileSync(resolve(__dirname, '../src/styles/theme.css'), 'utf8');
    expect(css).toMatch(/\.merged-text a,\s*\.merged-from a\s*\{[^}]*text-decoration:\s*underline/);
  });
});

test('UI-04 history event rows honour actor_kind / location_role sent on the timeline entry (AC-API-38)', async () => {
  server.use(
    http.get(`*/api/items/${SOURCE}/history`, () =>
      HttpResponse.json([{ at: '2026-10-18T00:06:31Z', kind: 'event', source: 'pipeline', actor_kind: 'operator', location_role: 'at', location_id: 'NOD1', summary: 'used NOD1 -> NOD3 by ui-review-2', image_url: null }])),
  );
  await openDetail();
  const list = await screen.findByRole('list', { name: /^history$/i });
  expect(list).toHaveTextContent('Used by operator ui-review-2 at NOD3');
});
