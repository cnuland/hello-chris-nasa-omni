// UI review round 3 on the pages (docs/ui-review/round3/):
//   UI3-03 "Person: <appearance>" in inventory rows, the detail title, review cards, the overview and chat citations;
//          person images are scene frames, never a crop endpoint;
//   UI3-04 person events with no "by <actor>"; one-sided moves without "to an unknown location";
//   UI3-05 person duplicate reviews: readable reason, appearance chips, "Whole frame, several people may be visible";
//   UI3-06 tile "Person tracks seen (last hour)"; UI3-07 crop caption; UI3-08 catalog names (slug in a tooltip);
//   UI3-09 one-row duplicate footer; UI3-11 wording (Unmerge, Pipeline update, short ids, mirrored rows hidden);
//   UI3-12 an h1 on the not-found pages.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { CatalogItem, Event, Item, ItemDetail, ReviewItem, TimelineEntry } from '../src/api/types';
import { CATALOG, ID, REVIEWS, drill, drillDetail } from './fixtures';
import { server, sseResponse } from './server';
import { findItem, findReviewCard, findToast, renderApp } from './render';

const P1 = '87dbd193-0000-4000-8000-000000000001';
const P2 = '0d53940f-0000-4000-8000-000000000002';
const P3 = 'f51af769-0000-4000-8000-000000000003';

const person = (id: string, extra: Partial<Item> = {}): Item => ({
  entity_id: id, catalog_id: 'crew-member', catalog_name: 'Crew member', kind: 'person', status: 'provisional',
  label: 'person', description: 'black shirt, grey pants', attributes: {}, last_location: 'USOS',
  last_location_name: 'US Orbital Segment', last_seen_at: '2026-10-18T03:14:00Z', confidence: 0.5,
  snapshot_url: `/media/frames/${id.slice(0, 4)}.jpg`, needs_review: false, ...extra,
});
const p1 = person(P1);
const p2 = person(P2, { description: null, snapshot_url: '/media/crops/p2-5.jpg' });
const CAT: CatalogItem[] = [...CATALOG, { catalog_id: 'crew-member', class_name: 'person', display_name: 'Crew member', category: 'person' }];

function itemsAre(items: Item[]) {
  server.use(http.get('*/api/items', () => HttpResponse.json({ items, total: items.length })));
}
function detailIs(item: ItemDetail, history: TimelineEntry[] = []) {
  server.use(
    http.get(`*/api/items/${item.entity_id}`, () => HttpResponse.json(item)),
    http.get(`*/api/items/${item.entity_id}/history`, () => HttpResponse.json(history)),
  );
}
function reviewsAre(items: ReviewItem[]) {
  server.use(http.get('*/api/review', () => HttpResponse.json(items)), http.get('*/api/catalog', () => HttpResponse.json(CAT)));
}
const imgSrcs = (el: HTMLElement) => Array.from(el.querySelectorAll('img')).map((i) => i.getAttribute('src') ?? '');

// ---------------------------------------------------------------- UI3-03
describe('UI3-03 person display label', () => {
  test('inventory rows read "Person: <appearance>", or "Person <short id>" without one', async () => {
    itemsAre([p1, p2]);
    renderApp('/?kind=person');
    const r1 = await findItem(P1);
    expect(within(r1).getByRole('link', { name: 'Person: black shirt, grey pants' })).toHaveAttribute('href', `/items/${P1}`);
    const r2 = await findItem(P2);
    expect(within(r2).getByRole('link', { name: 'Person 0d53940f' })).toBeInTheDocument();
    expect(within(r1).queryByRole('link', { name: /^person$/i })).toBeNull();
  });

  test('person thumbnails are scene frames; a crop URL is never requested for a person', async () => {
    itemsAre([p1, p2]);
    renderApp('/?kind=person');
    const r1 = await findItem(P1);
    const r2 = await findItem(P2);
    expect(imgSrcs(r1)).toEqual(['/api/media/frames/87db.jpg']);
    expect(imgSrcs(r2).some((s) => s.includes('/crops/'))).toBe(false);
  });

  test('the detail title is "Person: <appearance>" and the hero never shows a crop', async () => {
    detailIs({ ...p2, description: 'grey t-shirt, black shorts', events: [], sightings: [] });
    renderApp(`/items/${P2}`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Person: grey t-shirt, black shorts' })).toBeInTheDocument();
    expect(imgSrcs(screen.getByTestId('detail-media')).some((s) => s.includes('/crops/'))).toBe(false);
  });

  test('chat citations of a person use the person label (the agent label "person" is not enough)', async () => {
    detailIs({ ...p1, events: [], sightings: [] });
    server.use(http.post('*/agent/chat/stream', () => sseResponse([{ event: 'done', data: {
      reply: 'One person was seen.', citations: [{ entity_id: P1, at: '2026-10-18T03:14:00Z', label: 'person', location: 'USOS' }],
    } }])));
    const { user } = renderApp('/chat');
    await user.type(await screen.findByLabelText(/message/i), 'who was seen?');
    await user.click(screen.getByRole('button', { name: /send/i }));
    const cites = await screen.findByRole('list', { name: /citations/i });
    await waitFor(() => expect(within(cites).getByRole('link')).toHaveTextContent(/^Person: black shirt, grey pants/));
  });
});

// ---------------------------------------------------------------- UI3-04
const personEvent: Event = {
  event_id: 'e3000000-0000-4000-8000-000000000001', event_type: 'moved', entity_id: P1, entity_label: 'person',
  entity_catalog_id: 'crew-member', actor_desc: 'spacesuit, dark visor', actor_kind: 'person', from_location: 'NOD1',
  from_location_name: 'Node 1 Unity', to_location: null, occurred_at: '2026-10-18T03:10:00Z', source: 'pipeline',
};
const seenEvent: Event = {
  ...personEvent, event_id: 'e3000000-0000-4000-8000-000000000002', event_type: 'seen', entity_id: P2,
  actor_desc: 'grey t-shirt', from_location: null, from_location_name: null, to_location: 'CUP1', to_location_name: 'Cupola',
  location_role: 'at',
};
const oneSided: Event = {
  event_id: 'e3000000-0000-4000-8000-000000000003', event_type: 'moved', entity_id: ID.bag, entity_label: 'cargo transfer bag',
  entity_catalog_id: 'ctb-half', actor_desc: 'blue shirt', from_location: 'USOS', from_location_name: 'US Orbital Segment',
  to_location: null, occurred_at: '2026-10-18T03:09:00Z', source: 'pipeline',
};

describe('UI3-04 person events', () => {
  test('overview: a person event reads "left X" / "seen in X" with no "by <actor>"', async () => {
    server.use(http.get('*/api/events', () => HttpResponse.json([personEvent, seenEvent, oneSided])));
    renderApp('/overview');
    const feed = await screen.findByRole('list', { name: /recent events/i });
    const [a, b, c] = await within(feed).findAllByRole('listitem');
    expect(a).toHaveTextContent(/left Node 1 Unity \(NOD1\)/);
    expect(a.textContent).not.toMatch(/\bby\b|unknown location/);
    expect(within(a).getByRole('link')).toHaveTextContent(/^Person: spacesuit, dark visor/);
    expect(b).toHaveTextContent(/seen in Cupola \(CUP1\)/);
    expect(within(b).getByRole('link')).toHaveTextContent(/^Person: grey t-shirt/);
    expect(c).toHaveTextContent(/from US Orbital Segment \(USOS\) \(destination not seen\)/);
    expect(feed.textContent).not.toMatch(/unknown location/);
  });

  test('item detail Activity of a person has no actor clause', async () => {
    detailIs({ ...p1, events: [personEvent], sightings: [] });
    renderApp(`/items/${P1}`);
    const moves = await screen.findByRole('list', { name: /activity/i });
    expect(moves).toHaveTextContent(/^Left Node 1 Unity \(NOD1\) · 2026-10-18 03:10:00 UTC/);
    expect(moves.textContent).not.toMatch(/\bby\b/);
  });

  test('a person history event row (API summary) has no actor clause', async () => {
    detailIs({ ...p1, events: [], sightings: [] }, [
      { at: '2026-10-18T03:10:00Z', kind: 'event', event_type: 'moved', source: 'pipeline', summary: 'moved USOS -> ? by person (spacesuit, dark visor)' },
    ]);
    renderApp(`/items/${P1}`);
    const hist = await screen.findByRole('list', { name: /^history$/i });
    expect(hist).toHaveTextContent(/Left USOS/);
    expect(hist.textContent).not.toMatch(/\bby\b/);
  });

  test('inventory Last handled of a person row has no "by person (…)"', async () => {
    itemsAre([{ ...p1, last_handled: { event_type: 'moved', actor_desc: 'spacesuit, dark visor', occurred_at: '2026-10-18T03:10:00Z' } }]);
    renderApp('/?kind=person');
    const row = await findItem(P1);
    expect(row.textContent).not.toMatch(/by person|spacesuit/);
    expect(row).toHaveTextContent(/Seen moving/);
  });
});

// ---------------------------------------------------------------- UI3-05 / UI3-08
const INTERNAL = "person: grey hoodie (15 live 'person' entities would exceed the expected count of 7)";
const personDup: ReviewItem = {
  ...REVIEWS[1], review_id: ID.rvDup, kind: 'possible_duplicate', entity_id: P1, description: INTERNAL, model_guess: 'person',
  image_url: '/media/frames/meal.jpg', image_bbox: null, image_bbox_source: null,
  subject: { entity_id: P1, label: 'person', catalog_id: 'crew-member', last_location: 'USOS', thumbnail_url: '/media/frames/meal.jpg' },
  candidates: [
    { entity_id: P2, label: 'person', catalog_id: 'crew-member', description: 'grey hoodie, black pants', score: 0.37, thumbnail_url: '/media/frames/a.jpg' },
    { entity_id: P3, label: 'person', catalog_id: 'crew-member', score: 0.27, thumbnail_url: '/media/frames/b.jpg' },
  ],
  candidates_source: 'suggested',
};

describe('UI3-05 person duplicate reviews', () => {
  test('the reason is readable; the internal expected-count text is never shown', async () => {
    reviewsAre([personDup]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    expect(card).toHaveTextContent('More person tracks than the expected crew size — these may be the same person');
    expect(card.textContent).not.toMatch(/live 'person'|entities would exceed|expected count of/);
  });

  test('subject and candidates are named by appearance and show appearance chips side by side', async () => {
    reviewsAre([personDup]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const subject = within(card).getByRole('region', { name: /^subject/i });
    expect(within(subject).getByRole('link', { name: 'Person: grey hoodie' })).toBeInTheDocument();
    expect(within(within(subject).getByRole('list', { name: /appearance/i })).getAllByRole('listitem').map((l) => l.textContent)).toEqual(['grey hoodie']);
    const list = within(card).getByRole('list', { name: /candidates/i });
    const [c1, c2] = Array.from(list.children) as HTMLElement[];
    expect(within(c1).getByRole('link', { name: 'Person: grey hoodie, black pants' })).toBeInTheDocument();
    expect(within(within(c1).getByRole('list', { name: /appearance/i })).getAllByRole('listitem').map((l) => l.textContent))
      .toEqual(['grey hoodie', 'black pants']);
    expect(within(c2).getByRole('link', { name: 'Person f51af769' })).toBeInTheDocument();
  });

  test('a person review without a box is captioned "Whole frame, several people may be visible"', async () => {
    reviewsAre([personDup]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const subject = within(card).getByRole('region', { name: /^subject/i });
    expect(subject).toHaveTextContent('Whole frame, several people may be visible');
  });

  test('UI3-08 catalog lines show the display name, with the slug only in a tooltip', async () => {
    reviewsAre([{ ...personDup, subject: { ...personDup.subject!, catalog_id: 'ctb-half', label: 'bag' }, description: 'bag: white', model_guess: 'bag' }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    await waitFor(() => expect(within(card).getAllByText('Catalog: Cargo transfer bag (half)')[0]).toHaveAttribute('title', 'ctb-half'));
    expect(within(card).getAllByText('Catalog: Crew member')[0]).toHaveAttribute('title', 'crew-member');
    expect(card.textContent).not.toMatch(/Catalog: (ctb-half|crew-member)|catalog ctb-half/);
  });
});

// ---------------------------------------------------------------- UI3-09
describe('UI3-09 compact duplicate footer', () => {
  test('the footer is one row of buttons; the catalog search opens from "Choose catalog…"', async () => {
    reviewsAre([personDup]);
    const { user } = renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const footer = card.querySelector(':scope > .review-footer') as HTMLElement;
    const row = footer.querySelector(':scope .review-footer-row') as HTMLElement;
    expect(row).not.toBeNull();
    expect(within(row).getByRole('button', { name: /keep both/i })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: /dismiss/i })).toBeInTheDocument();
    expect(within(footer).queryByLabelText(/search catalog/i)).toBeNull();
    const toggle = within(row).getByRole('button', { name: /choose catalog/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(within(footer).getByLabelText(/search catalog/i)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- UI3-06 / UI3-07
describe('UI3-06 / UI3-07', () => {
  test('the overview tile is "Person tracks seen (last hour)"', async () => {
    renderApp('/overview');
    expect(await screen.findByRole('region', { name: 'Person tracks seen (last hour)' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: /^people seen/i })).toBeNull();
  });

  test('a crop hero is captioned "Close-up crop" and rendered at natural size', async () => {
    detailIs({ ...drillDetail, snapshot_url: '/media/crops/2348f197-5.jpg', snapshot_bbox: null });
    renderApp(`/items/${ID.drill}`);
    const media = await screen.findByTestId('detail-media');
    expect(media).toHaveTextContent(/Close-up crop/);
    expect(media.textContent).not.toMatch(/whole frame/i);
    expect(media.querySelector('.frame-trigger')).toHaveClass('frame-crop');
  });
});

// ---------------------------------------------------------------- UI3-11
describe('UI3-11 wording', () => {
  test('history shows the audit op "split" as "Unmerge"', async () => {
    detailIs(drillDetail, [{ at: '2026-10-18T03:30:00Z', kind: 'audit', summary: 'split by ui-review-3' }]);
    renderApp(`/items/${ID.drill}`);
    const hist = await screen.findByRole('list', { name: /^history$/i });
    expect(hist).toHaveTextContent('Unmerge by ui-review-3');
    expect(hist.textContent).not.toMatch(/split/);
  });

  test('a pipeline update that only mirrors a sighting is hidden', async () => {
    detailIs(drillDetail, [
      { at: '2026-10-18T03:30:01Z', kind: 'audit', summary: 'update by pipeline' },
      { at: '2026-10-18T03:30:00Z', kind: 'sighting', summary: 'seen in LAB1', location_id: 'LAB1' },
      { at: '2026-10-18T02:00:00Z', kind: 'audit', summary: 'update by pipeline' },
    ]);
    renderApp(`/items/${ID.drill}`);
    const hist = await screen.findByRole('list', { name: /^history$/i });
    expect(within(hist).getAllByRole('listitem')).toHaveLength(2);
    expect(hist).toHaveTextContent('Pipeline update');
    expect(screen.getByRole('button', { name: /^audit \(1\)/i })).toBeInTheDocument();
  });

  test('the merged banner and the unmerge toast name items with their short id', async () => {
    window.localStorage.setItem('nasa-omni.operator', 'ui-review-3');
    let unmerged = false;
    const merged: ItemDetail = { ...drillDetail, status: 'merged', merged_into: { entity_id: ID.bag, label: 'cargo transfer bag' }, merged_at: '2026-10-18T00:07:10Z' };
    server.use(
      http.get(`*/api/items/${ID.drill}`, () => HttpResponse.json(unmerged ? drillDetail : merged)),
      http.post(`*/api/items/${ID.drill}/unmerge`, () => {
        unmerged = true;
        return HttpResponse.json([drill]);
      }),
    );
    const { user } = renderApp(`/items/${ID.drill}`);
    const banner = await screen.findByRole('region', { name: /merged/i });
    expect(banner).toHaveTextContent(/Merged into cargo transfer bag 44444444 at 2026-10-18 00:07 UTC/);
    await user.click(within(banner).getByRole('button', { name: /^unmerge/i }));
    expect(await findToast(/Unmerged: Pistol-grip drill 11111111 is a separate item again\./)).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- UI3-12
describe('UI3-12 not-found pages have an h1', () => {
  test('/nope', async () => {
    renderApp('/nope');
    expect(await screen.findByRole('heading', { level: 1, name: 'Page not found' })).toBeInTheDocument();
  });

  test('/items/<missing id>', async () => {
    renderApp('/items/00000000-0000-4000-8000-000000000000');
    expect(await screen.findByRole('heading', { level: 1, name: 'Item not found' })).toBeInTheDocument();
  });
});
