// UI review round 4 (docs/ui-review/round4/), dashboard side:
//   UI4-02 crops never exceed their container (CSS rules: tests/ui4_layout_css.test.tsx; pixels: scripts/ui_check.py);
//   UI4-11 a crop under 200 px on its long side is not the detail hero: the best whole frame is, with the crop inset;
//   UI4-05 "Not inventory (station structure)" quick action on unknown cards (dismiss + note "not_inventory");
//   UI4-06 overview events: "Person: <appearance>", always a place, no "left X" while still in X, object places;
//   UI3-11 sentence-case audit labels; UI4-07 module / segment-only counts; UI4-10 private-window "no image" reason;
//   UI4-09 the chat status timer starts at 0 s for every question.
// Every new API field is optional: each behaviour is tested with and without it.
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test, vi } from 'vitest';
import type { Event, Item, ItemDetail, ReviewItem } from '../src/api/types';
import { auditText } from '../src/pages/ItemDetailPage';
import { ID, REVIEWS, STATS, drill } from './fixtures';
import { controllableSSE, requestsTo, server } from './server';
import { fakeTimers, findReviewCard, findToast, renderApp } from './render';

const P1 = '737f632b-0000-4000-8000-000000000001';
const P2 = '64db166f-0000-4000-8000-000000000002';
const P3 = '54b2c660-0000-4000-8000-000000000003';
const LAPTOP = 'd977201c-0000-4000-8000-000000000004';
const CAM = 'cb9268bb-0000-4000-8000-000000000005';
const SMALL = '03e45b90-0000-4000-8000-000000000006';

function eventsAre(events: Event[]) {
  server.use(http.get('*/api/events', () => HttpResponse.json(events)));
}
async function feedRows(n: number) {
  const feed = await screen.findByRole('list', { name: /recent events/i });
  await waitFor(() => expect(within(feed).getAllByRole('listitem')).toHaveLength(n));
  return within(feed).getAllByRole('listitem');
}
const personEv = (id: string, extra: Partial<Event> & Record<string, unknown> = {}): Event => ({
  event_id: `e-${id}-${Object.keys(extra).length}`, event_type: 'moved', entity_id: id, entity_label: 'person',
  entity_catalog_id: 'crew-member', actor_desc: null, occurred_at: '2026-10-18T04:38:00Z', source: 'pipeline', ...extra,
} as Event);

// ---------------------------------------------------------------- UI4-06
describe('UI4-06 overview person events', () => {
  test('the label is "Person: <entity_appearance>"', async () => {
    eventsAre([personEv(P1, { event_type: 'seen', location_role: 'at', to_location: 'CUP1', to_location_name: 'Cupola',
      entity_appearance: 'blue shirt and white gloves' })]);
    renderApp('/overview');
    const [row] = await feedRows(1);
    expect(within(row).getByRole('link')).toHaveTextContent(/^Person: blue shirt and white gloves/);
    expect(row).toHaveTextContent(/seen in Cupola \(CUP1\)/);
  });

  test('without entity_appearance the label falls back to the cached item description', async () => {
    server.use(http.get(`*/api/items/${P2}`, () => HttpResponse.json({
      ...drill, entity_id: P2, kind: 'person', catalog_id: 'crew-member', label: 'person', description: 'grey hoodie, black pants',
      last_location: 'NOD3', last_location_name: 'Node 3 Tranquility', events: [], sightings: [],
    })));
    eventsAre([personEv(P2, { event_type: 'seen', location_role: 'at' })]);
    renderApp('/overview');
    const [row] = await feedRows(1);
    await waitFor(() => expect(within(row).getByRole('link')).toHaveTextContent(/^Person: grey hoodie, black pants/));
    // no place on the event: the item's last location is used
    expect(row).toHaveTextContent(/seen in Node 3 Tranquility \(NOD3\)/);
  });

  test('an object item\'s description is never used as a person\'s appearance (cycle 2, found by Playwright)', async () => {
    server.use(http.get(`*/api/items/${P2}`, () => HttpResponse.json({ ...drill, entity_id: P2, description: 'Black, handheld', events: [], sightings: [] })));
    eventsAre([personEv(P2, { event_type: 'seen', location_role: 'at', to_location: 'CUP1', to_location_name: 'Cupola' })]);
    renderApp('/overview');
    const [row] = await feedRows(1);
    await waitFor(() => expect(requestsTo('GET', `/api/items/${P2}`)).toHaveLength(1));
    await new Promise((r) => setTimeout(r, 30));
    expect(within(row).getByRole('link')).toHaveTextContent(/^Person 64db166f/);
    expect(row.textContent).not.toMatch(/handheld/i);
  });

  test('"seen" with no place uses entity_last_location; with nothing known it says the place was not seen', async () => {
    eventsAre([
      personEv(P1, { event_type: 'seen', location_role: 'at', entity_appearance: 'grey t-shirt', entity_last_location: 'CUP1',
        entity_last_location_name: 'Cupola' }),
      personEv(P3, { event_id: 'e-p3', event_type: 'seen', location_role: 'at', entity_appearance: 'blue shorts' }),
    ]);
    renderApp('/overview');
    const [a, b] = await feedRows(2);
    expect(a).toHaveTextContent(/seen in Cupola \(CUP1\)/);
    await waitFor(() => expect(b).toHaveTextContent(/seen \(place not seen\)/));
    expect(b.textContent).not.toMatch(/seen\s+2026/);
  });

  test('"left X" reads "seen in X" when the track\'s last location is still X', async () => {
    eventsAre([
      personEv(P1, { from_location: 'USOS', from_location_name: 'US Orbital Segment', to_location: null, entity_appearance: 'blue shirt',
        entity_last_location: 'USOS', entity_last_location_name: 'US Orbital Segment' }),
      personEv(P3, { event_id: 'e-p3', from_location: 'NOD1', from_location_name: 'Node 1 Unity', to_location: null,
        entity_appearance: 'orange top', entity_last_location: 'NOD3', entity_last_location_name: 'Node 3 Tranquility' }),
    ]);
    renderApp('/overview');
    const [a, b] = await feedRows(2);
    expect(a).toHaveTextContent(/seen in US Orbital Segment \(USOS\)/);
    expect(a.textContent).not.toMatch(/\bleft\b/);
    expect(b).toHaveTextContent(/left Node 1 Unity \(NOD1\)/);
  });

  test('older events (no new fields, item not loadable) keep the round-3 wording', async () => {
    eventsAre([personEv(P3, { from_location: 'NOD1', from_location_name: 'Node 1 Unity', actor_desc: 'spacesuit, dark visor' })]);
    renderApp('/overview');
    const [row] = await feedRows(1);
    expect(row).toHaveTextContent(/left Node 1 Unity \(NOD1\)/);
    expect(within(row).getByRole('link')).toHaveTextContent(/^Person: spacesuit, dark visor/);
  });
});

describe('UI4-06 overview object events state a place', () => {
  test('"laptop used in Node 1 Unity (NOD1) by person (grey t-shirt)"', async () => {
    eventsAre([{ event_id: 'o1', event_type: 'used', entity_id: LAPTOP, entity_label: 'laptop', actor_desc: 'grey t-shirt',
      actor_kind: 'person', from_location: 'NOD1', from_location_name: 'Node 1 Unity', location_role: 'at',
      occurred_at: '2026-10-18T04:41:00Z', source: 'pipeline' }]);
    renderApp('/overview');
    const [row] = await feedRows(1);
    expect(row.textContent).toMatch(/laptop d977201c\s*used in Node 1 Unity \(NOD1\) by person \(grey t-shirt\)/);
  });

  test('an event without a place uses entity_last_location, else says the place was not seen', async () => {
    eventsAre([
      { event_id: 'o2', event_type: 'used', entity_id: LAPTOP, entity_label: 'laptop', actor_desc: 'grey t-shirt', actor_kind: 'person',
        occurred_at: '2026-10-18T04:41:00Z', source: 'pipeline', entity_last_location: 'NOD1', entity_last_location_name: 'Node 1 Unity' } as Event,
      { event_id: 'o3', event_type: 'moved', entity_id: CAM, entity_label: 'camera', occurred_at: '2026-10-18T04:34:00Z', source: 'pipeline' },
    ]);
    renderApp('/overview');
    const [a, b] = await feedRows(2);
    expect(a).toHaveTextContent(/used \(last seen in Node 1 Unity \(NOD1\)\) by person \(grey t-shirt\)/);
    expect(b).toHaveTextContent(/camera cb9268bb\s*moved \(place not seen\)/);
  });
});

// ---------------------------------------------------------------- UI4-07
describe('UI4-07 module distribution', () => {
  test('shows module vs segment-only counts when by_location_level is sent', async () => {
    server.use(http.get('*/api/stats', () => HttpResponse.json({ ...STATS, by_location_level: { module: 35, segment: 30, unknown: 0 } })));
    renderApp('/overview');
    expect(await screen.findByText('Located to a module: 35 · Segment only (USOS/ROS): 30')).toBeInTheDocument();
  });

  test('also counts items with no location when there are any', async () => {
    server.use(http.get('*/api/stats', () => HttpResponse.json({ ...STATS, by_location_level: { module: 3, segment: 2, unknown: 4 } })));
    renderApp('/overview');
    expect(await screen.findByText(/Located to a module: 3 · Segment only \(USOS\/ROS\): 2 · No location: 4/)).toBeInTheDocument();
  });

  test('older stats without by_location_level show no such line', async () => {
    renderApp('/overview');
    await screen.findByRole('list', { name: /recent events/i });
    await waitFor(() => expect(screen.getByLabelText('Items')).toHaveTextContent(/3/));
    expect(screen.queryByText(/Located to a module/)).toBeNull();
  });
});

// ---------------------------------------------------------------- UI3-11
describe('UI3-11 sentence-case audit labels', () => {
  test('merge / create / update / split read "Merged by", "Created by pipeline", "Pipeline update", "Unmerged by"', () => {
    expect(auditText('merge by ui-review-4')).toBe('Merged by ui-review-4');
    expect(auditText('create by pipeline')).toBe('Created by pipeline');
    expect(auditText('update by pipeline')).toBe('Pipeline update');
    expect(auditText('split by ui-review-4')).toBe('Unmerged by ui-review-4');
  });

  test('other ops are sentence case too and keep their detail', () => {
    expect(auditText('dismiss by privacy-purge: removed for crew privacy')).toBe('Dismissed by privacy-purge: removed for crew privacy');
    expect(auditText('classify by ui-review-4')).toBe('Classified by ui-review-4');
    expect(auditText('update by ui-review-4 (last_location: USOS -> LAB1)')).toBe('Updated by ui-review-4 (last_location: USOS -> LAB1)');
    expect(auditText('something else')).toBe('Something else');
  });
});

// ---------------------------------------------------------------- UI4-10 / UI4-11 (item detail hero)
const smallCrop: ItemDetail = {
  ...drill, entity_id: SMALL, label: 'camera', snapshot_url: '/media/crops/cam/76x62.jpg', snapshot_bbox: null, events: [],
  sightings: [
    { sighting_id: 's1', seen_at: '2026-10-18T04:00:00Z', location_id: 'CUP1', frame_url: '/media/frames/old.jpg' },
    { sighting_id: 's2', seen_at: '2026-10-18T04:41:00Z', location_id: 'CUP1', frame_url: '/media/frames/new.jpg' },
  ],
};
function detailIs(item: ItemDetail | Item) {
  server.use(
    http.get(`*/api/items/${item.entity_id}`, () => HttpResponse.json(item)),
    http.get(`*/api/items/${item.entity_id}/history`, () => HttpResponse.json([])),
  );
}
function loadImg(img: HTMLImageElement, w: number, h: number) {
  Object.defineProperty(img, 'naturalWidth', { configurable: true, value: w });
  Object.defineProperty(img, 'naturalHeight', { configurable: true, value: h });
  fireEvent.load(img);
}
const heroImg = async () => {
  const media = await screen.findByTestId('detail-media');
  return { media, img: await waitFor(() => media.querySelector('img') as HTMLImageElement) };
};

describe('UI4-11 small crops are not the detail hero', () => {
  test('a 76×62 crop: the latest whole frame is the hero, the crop is an inset close-up', async () => {
    detailIs(smallCrop);
    renderApp(`/items/${SMALL}`);
    const { media, img } = await heroImg();
    loadImg(img, 76, 62);
    await waitFor(() => expect(media.querySelector('.hero-frame img')).toHaveAttribute('src', '/api/media/frames/new.jpg'));
    const inset = media.querySelector('.hero-inset') as HTMLElement;
    expect(inset).not.toBeNull();
    expect(inset.querySelector('img')).toHaveAttribute('src', '/api/media/crops/cam/76x62.jpg');
    expect(inset).toHaveTextContent(/close-up/i);
  });

  test('a frame with a box is preferred over a newer one without', async () => {
    detailIs({ ...smallCrop, sightings: [{ ...smallCrop.sightings![0], bbox: [0.1, 0.1, 0.3, 0.3], bbox_source: 'detector' }, smallCrop.sightings![1]] });
    renderApp(`/items/${SMALL}`);
    const { media, img } = await heroImg();
    loadImg(img, 76, 62);
    await waitFor(() => expect(media.querySelector('.hero-frame img')).toHaveAttribute('src', '/api/media/frames/old.jpg'));
    expect(media.querySelector('.hero-frame [data-testid="bbox"]')).not.toBeNull();
  });

  test('a crop of 200 px or more on its long side stays the hero', async () => {
    detailIs(smallCrop);
    renderApp(`/items/${SMALL}`);
    const { media, img } = await heroImg();
    loadImg(img, 240, 90);
    await new Promise((r) => setTimeout(r, 20));
    expect(media.querySelector('.hero-inset')).toBeNull();
    expect(media.querySelector('img')).toHaveAttribute('src', '/api/media/crops/cam/76x62.jpg');
  });

  test('no frame: the small crop is shown at up to 2× with a "small close-up" caption', async () => {
    detailIs({ ...smallCrop, sightings: [] });
    renderApp(`/items/${SMALL}`);
    const { media, img } = await heroImg();
    loadImg(img, 76, 62);
    await waitFor(() => expect(media).toHaveTextContent(/small close-up/i));
    const shown = media.querySelector('img') as HTMLImageElement;
    expect(shown).toHaveAttribute('src', '/api/media/crops/cam/76x62.jpg');
    expect(shown.style.width).toBe('152px');
  });
});

describe('UI4-10 private-window items explain the missing image', () => {
  test('snapshot_withheld_reason "private_window" shows the crew-privacy reason', async () => {
    detailIs({ ...drill, snapshot_url: null, snapshot_withheld_reason: 'private_window' } as Item);
    renderApp(`/items/${ID.drill}`);
    const media = await screen.findByTestId('detail-media');
    expect(media).toHaveTextContent('No image: first seen in a private area (crew privacy)');
    expect(media.textContent).not.toMatch(/^no image$/);
  });

  test('without a reason the placeholder stays "no image"', async () => {
    detailIs({ ...drill, snapshot_url: null, snapshot_withheld_reason: null } as Item);
    renderApp(`/items/${ID.drill}`);
    const media = await screen.findByTestId('detail-media');
    expect(media).toHaveTextContent(/^no image$/);
    expect(media.textContent).not.toMatch(/privacy/);
  });
});

// ---------------------------------------------------------------- UI4-05
const structure: ReviewItem = { ...REVIEWS[0], entity_id: ID.window1, description: 'hatch: a round hatch with a handle', model_guess: 'hatch' };
async function openUnknowns(operator = 'ui-review-4') {
  if (operator) window.localStorage.setItem('nasa-omni.operator', operator);
  server.use(http.get('*/api/review', () => HttpResponse.json([structure, REVIEWS[1]])));
  const r = renderApp('/review');
  return { ...r, card: await findReviewCard(structure.review_id) };
}
const NOT_INV = /^not inventory \(station structure\)/i;

describe('UI4-05 "Not inventory (station structure)" on unknown cards', () => {
  test('the first click posts nothing and opens a confirm step that says what happens', async () => {
    const { user, card } = await openUnknowns();
    await user.click(within(card).getByRole('button', { name: NOT_INV }));
    await new Promise((r) => setTimeout(r, 30));
    expect(requestsTo('POST', `/api/review/${structure.review_id}/resolve`)).toHaveLength(0);
    const group = within(card).getByRole('group', { name: /confirm not inventory/i });
    expect(group).toHaveTextContent(/station structure/i);
    expect(group).toHaveTextContent(/hides it from the default inventory/i);
  });

  test('confirming posts dismiss with the note "not_inventory" and the operator, then removes the card', async () => {
    const { user, card } = await openUnknowns();
    await user.click(within(card).getByRole('button', { name: NOT_INV }));
    await user.click(within(card).getByRole('button', { name: /confirm: not inventory/i }));
    await waitFor(() => expect(requestsTo('POST', `/api/review/${structure.review_id}/resolve`)).toHaveLength(1));
    expect(requestsTo('POST', `/api/review/${structure.review_id}/resolve`)[0].body).toEqual({
      action: 'dismiss', note: 'not_inventory', operator: 'ui-review-4',
    });
    expect(await findToast(/station structure/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId(`review-${structure.review_id}`)).toBeNull());
  });

  test('needs an operator name', async () => {
    const { card } = await openUnknowns('');
    expect(within(card).getByRole('button', { name: NOT_INV })).toBeDisabled();
  });

  test('Cancel closes the step; opening it closes the plain dismiss step', async () => {
    const { user, card } = await openUnknowns();
    await user.click(within(card).getByRole('button', { name: /^dismiss/i }));
    await user.click(within(card).getByRole('button', { name: NOT_INV }));
    expect(within(card).queryByRole('group', { name: /confirm dismiss/i })).toBeNull();
    await user.click(within(card).getByRole('button', { name: /^cancel$/i }));
    expect(within(card).queryByRole('group', { name: /confirm not inventory/i })).toBeNull();
    expect(requestsTo('POST', /\/resolve$/)).toHaveLength(0);
  });

  test('duplicate cards do not get the action', async () => {
    await openUnknowns();
    const dup = await findReviewCard(ID.rvDup);
    expect(within(dup).queryByRole('button', { name: NOT_INV })).toBeNull();
  });
});

// ---------------------------------------------------------------- UI4-09
describe('UI4-09 chat status timer', () => {
  test('the second question never shows the first question\'s elapsed seconds', async () => {
    fakeTimers(new Date('2026-10-18T05:00:00Z'));
    const first = controllableSSE();
    server.use(http.post('*/agent/chat/stream', () => first.response()));
    const { user } = renderApp('/chat', { fakeTimers: true });
    await user.type(await screen.findByLabelText(/message/i), 'Where is the laptop?');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await screen.findByRole('status');
    await vi.advanceTimersByTimeAsync(7_000);
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(/7 s/));
    first.push({ event: 'done', data: { reply: 'In NOD1.', citations: [] } });
    first.close();
    await screen.findByText('In NOD1.');

    const second = controllableSSE();
    server.use(http.post('*/agent/chat/stream', () => second.response()));
    // Every value the counter ever shows (including ones replaced before the next paint).
    const shown: string[] = [];
    const obs = new MutationObserver((records) => {
      for (const r of records) {
        if (r.type === 'characterData' && r.oldValue) shown.push(r.oldValue);
        r.addedNodes.forEach((n) => n.textContent && shown.push(n.textContent));
        r.removedNodes.forEach((n) => n.textContent && shown.push(n.textContent));
      }
    });
    obs.observe(document.body, { subtree: true, childList: true, characterData: true, characterDataOldValue: true });
    await user.type(screen.getByLabelText(/message/i), 'And the drill?');
    await user.click(screen.getByRole('button', { name: /send/i }));
    await screen.findByRole('status');
    await Promise.resolve();
    obs.disconnect();
    const counters = shown.flatMap((t) => [...t.matchAll(/(\d+) s\b/g)].map((m) => Number(m[1])));
    expect(counters.length).toBeGreaterThan(0);
    expect(counters.filter((n) => n >= 7)).toEqual([]);
    second.close();
  });
});
