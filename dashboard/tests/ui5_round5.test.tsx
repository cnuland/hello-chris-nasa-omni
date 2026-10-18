// UI review round 5 (docs/ui-review/round5/), dashboard side:
//   UI5-06 every history frame opens the lightbox (boxless sightings too);
//   UI5-09 person tracks show "Last seen in <place>" (never "handled" wording); a move with no from_location reads
//          "First seen in X" (until perception / inventory-api stop emitting it);
//   UI5-10 person hero caption names crew privacy; the audit op "split" reads "Unmerged" in every form; item labels in
//          sentence case where the label is a row / card title (raw label kept in a tooltip);
//   UI5-01 boxless unknowns: "No box: look for <label> in the frame"; "Same frame as N other unknowns";
//   UI5-02 "Not inventory" bulk action on the unknown tab (checkboxes, confirm step, operator, keyboard).
// Every new API field is optional: each behaviour is tested with and without it.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { Event, Item, ItemDetail, ReviewItem, TimelineEntry } from '../src/api/types';
import { eventSentence, legacyEventSummary, objectEventSentence, sentenceCase } from '../src/lib/format';
import { displayLabel } from '../src/lib/person';
import { auditText } from '../src/pages/ItemDetailPage';
import { ID, REVIEWS, drill } from './fixtures';
import { requestsTo, server } from './server';
import { findItem, findReviewCard, findToast, renderApp } from './render';

const P1 = '395bae46-0000-4000-8000-000000000001';
const LAPTOP = 'e6f810c9-0000-4000-8000-000000000002';
const CAM = '1cb6165e-0000-4000-8000-000000000003';

function itemsAre(items: Item[]) {
  server.use(http.get('*/api/items', () => HttpResponse.json({ items, total: items.length })));
}
function detailIs(item: ItemDetail, history: TimelineEntry[] = []) {
  server.use(
    http.get(`*/api/items/${item.entity_id}`, () => HttpResponse.json(item)),
    http.get(`*/api/items/${item.entity_id}/history`, () => HttpResponse.json(history)),
  );
}
const person: ItemDetail = {
  ...drill, entity_id: P1, kind: 'person', catalog_id: 'crew-member', catalog_name: 'Crew member', label: 'person',
  description: 'blue shirt, olive trousers', last_location: 'CUP1', last_location_name: 'Cupola',
  last_seen_at: '2026-10-18T06:31:08Z', snapshot_url: '/media/frames/063108.jpg', snapshot_bbox: null,
  last_handled: { event_type: 'moved', actor_desc: null, occurred_at: '2026-10-18T06:31:00Z' }, events: [], sightings: [],
};

// ---------------------------------------------------------------- UI5-06
describe('UI5-06 boxless history frames open the lightbox', () => {
  const laptop: ItemDetail = { ...drill, entity_id: LAPTOP, label: 'laptop', events: [],
    sightings: [{ sighting_id: 's1', seen_at: '2026-10-18T06:28:00Z', frame_url: '/media/frames/062800.jpg', bbox: null }] };
  const hist: TimelineEntry[] = [
    { at: '2026-10-18T06:28:00Z', kind: 'sighting', id: 's1', summary: 'Seen at JPM1', image_url: '/media/frames/062800.jpg' },
    { at: '2026-10-18T06:29:00Z', kind: 'sighting', summary: 'Seen at JPM1', image_url: '/media/frames/062900.jpg' },
  ];

  test('a sighting with no box is an enlarge button and opens the dialog with the whole-frame caption', async () => {
    detailIs(laptop, hist);
    const { user } = renderApp(`/items/${LAPTOP}`);
    const histList = await screen.findByRole('list', { name: /^history$/i });
    const buttons = await within(histList).findAllByRole('button', { name: /enlarge image/i });
    expect(buttons).toHaveLength(2);
    await user.click(buttons[0]);
    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent(/whole frame/i);
    expect(dialog.querySelector('[data-testid="bbox"]')).toBeNull();
  });

  test('a person\'s history frame opens with the crew-privacy caption, and crop URLs stay placeholders', async () => {
    detailIs(person, [
      { at: '2026-10-18T06:31:08Z', kind: 'sighting', summary: 'Seen at CUP1', image_url: '/media/frames/063108.jpg' },
      { at: '2026-10-18T06:30:00Z', kind: 'sighting', summary: 'Seen at NOD1', image_url: '/media/crops/p1.jpg' },
    ]);
    const { user } = renderApp(`/items/${P1}`);
    const histList = await screen.findByRole('list', { name: /^history$/i });
    const buttons = await within(histList).findAllByRole('button', { name: /enlarge image/i });
    expect(buttons).toHaveLength(1);
    expect(histList.querySelector('img[src*="/crops/"]')).toBeNull();
    await user.click(buttons[0]);
    expect(await screen.findByRole('dialog')).toHaveTextContent(/person tracks are not boxed or cropped, for crew privacy/i);
  });
});

// ---------------------------------------------------------------- UI5-09
describe('UI5-09 person tracks: "Last seen in <place>", never "handled"', () => {
  test('inventory row of a person reads "Last seen in Cupola (CUP1)" with the time', async () => {
    itemsAre([person]);
    renderApp('/?kind=person');
    const row = await findItem(P1);
    expect(row).toHaveTextContent(/Last seen in Cupola \(CUP1\)/);
    expect(row).toHaveTextContent(/2026-10-18 06:31 UTC/);
    expect(row.textContent).not.toMatch(/Seen moving|handled|Moved/i);
  });

  test('a person with no location reads "Last seen (place not recorded)"', async () => {
    itemsAre([{ ...person, last_location: null, last_location_name: null }]);
    renderApp('/?kind=person');
    const row = await findItem(P1);
    expect(row).toHaveTextContent(/Last seen \(place not recorded\)/);
  });

  test('a person\'s detail page has no "Last handled" wording', async () => {
    detailIs(person);
    renderApp(`/items/${P1}`);
    await screen.findByRole('heading', { level: 1 });
    const facts = document.querySelector('dl.facts') as HTMLElement;
    expect(facts.textContent).not.toMatch(/handled|Seen moving/i);
    expect(facts).toHaveTextContent(/Last seen in Cupola \(CUP1\)/);
  });

  test('an object keeps "Last handled" and its actor wording', async () => {
    itemsAre([{ ...drill, last_handled: { event_type: 'used', actor_desc: 'blue shirt', occurred_at: '2026-10-17T11:00:00Z' } }]);
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(row).toHaveTextContent(/Used by person \(blue shirt\)/);
  });
});

describe('UI5-09 a move with no from_location reads "First seen in X"', () => {
  const firstMove: Event = { event_id: 'fm', event_type: 'moved', entity_id: CAM, entity_label: 'camera', actor_desc: null,
    from_location: null, to_location: 'USOS', to_location_name: 'US Orbital Segment', location_role: 'from_to',
    occurred_at: '2026-10-18T06:20:00Z', source: 'pipeline' };

  test('eventSentence / objectEventSentence / legacy summary', () => {
    expect(eventSentence(firstMove)).toBe('First seen in US Orbital Segment (USOS)');
    expect(objectEventSentence(firstMove)).toBe('first seen in US Orbital Segment (USOS)');
    expect(legacyEventSummary('moved ? -> NOD1')).toBe('First seen in NOD1');
  });

  test('an operator placing an item with no previous location keeps "Moved by operator …"', () => {
    expect(eventSentence({ ...firstMove, actor_desc: 'ui-review-5', actor_kind: 'operator', source: 'operator' }))
      .toBe('Moved by operator ui-review-5 to US Orbital Segment (USOS)');
  });

  test('two-sided moves are unchanged', () => {
    expect(eventSentence({ ...firstMove, from_location: 'NOD1', from_location_name: 'Node 1 Unity' }))
      .toBe('Moved by an unknown person from Node 1 Unity (NOD1) to US Orbital Segment (USOS)');
  });

  test('Activity on the detail page and the overview row', async () => {
    detailIs({ ...drill, entity_id: CAM, label: 'camera', events: [firstMove], sightings: [] });
    renderApp(`/items/${CAM}`);
    const act = await screen.findByRole('list', { name: /^activity$/i });
    expect(act).toHaveTextContent(/First seen in US Orbital Segment \(USOS\)/);
    expect(act.textContent).not.toMatch(/Moved by/);
  });

  test('the overview row says "first seen", not "moved"', async () => {
    server.use(http.get('*/api/events', () => HttpResponse.json([firstMove])));
    renderApp('/overview');
    const feed = await screen.findByRole('list', { name: /recent events/i });
    await waitFor(() => expect(within(feed).getAllByRole('listitem')).toHaveLength(1));
    expect(feed).toHaveTextContent(/first seen in US Orbital Segment \(USOS\)/);
    expect(feed.textContent).not.toMatch(/moved/i);
  });

  test('inventory Last handled: a pipeline move at first_seen_at with no actor reads "First seen in X"', async () => {
    itemsAre([{ ...drill, entity_id: CAM, label: 'camera', last_location: 'USOS', last_location_name: 'US Orbital Segment',
      first_seen_at: '2026-10-18T06:20:00Z',
      last_handled: { event_type: 'moved', actor_desc: null, occurred_at: '2026-10-18T06:20:01Z' } }]);
    renderApp('/');
    const row = await findItem(CAM);
    expect(row).toHaveTextContent(/First seen in US Orbital Segment \(USOS\)/);
    expect(row.textContent).not.toMatch(/Moved by/);
  });

  test('inventory Last handled: a later move keeps "Moved by …"', async () => {
    itemsAre([{ ...drill, entity_id: CAM, label: 'camera', first_seen_at: '2026-10-18T06:00:00Z',
      last_handled: { event_type: 'moved', actor_desc: null, occurred_at: '2026-10-18T06:20:00Z' } }]);
    renderApp('/');
    const row = await findItem(CAM);
    expect(row).toHaveTextContent(/Moved by an unknown person/);
  });
});

// ---------------------------------------------------------------- UI5-10
describe('UI5-10 copy polish', () => {
  test('a person hero says the whole frame is shown for crew privacy, not "no box for this object"', async () => {
    detailIs(person);
    renderApp(`/items/${P1}`);
    const media = await screen.findByTestId('detail-media');
    expect(media).toHaveTextContent('Whole frame (person tracks are not boxed or cropped, for crew privacy). Click to enlarge.');
    expect(media.textContent).not.toMatch(/no box for this object/);
  });

  test('an object hero keeps the whole-frame wording', async () => {
    detailIs({ ...drill, snapshot_url: '/media/frames/1.jpg', snapshot_bbox: null, events: [], sightings: [] });
    renderApp(`/items/${ID.drill}`);
    expect(await screen.findByTestId('detail-media')).toHaveTextContent(/no box for this object/);
  });

  test.each([
    ['split by ui-review-5', 'Unmerged by ui-review-5'],
    ['Split by ui-review-5', 'Unmerged by ui-review-5'],
    ['split', 'Unmerged'],
    ['split: restored', 'Unmerged: restored'],
    ['split (merged_into: x -> null)', 'Unmerged (merged_into: x -> null)'],
    ['unmerge', 'Unmerged'],
  ])('auditText(%j) = %j', (raw, want) => {
    expect(auditText(raw)).toBe(want);
  });

  test('the folded raw audit list says "Unmerged", never "split"', async () => {
    detailIs({ ...drill, events: [], sightings: [] }, [
      { at: '2026-10-18T06:40:00Z', kind: 'audit', summary: 'split by ui-review-5' },
      { at: '2026-10-18T06:39:00Z', kind: 'audit', summary: 'split by ui-review-5' },
    ]);
    const { user } = renderApp(`/items/${ID.drill}`);
    const histList = await screen.findByRole('list', { name: /^history$/i });
    await user.click(within(histList).getByRole('button', { name: /show 2 entries/i }));
    expect(histList.textContent).not.toMatch(/split/i);
    expect(within(histList).getAllByText(/Unmerged by ui-review-5/).length).toBeGreaterThanOrEqual(2);
  });

  test.each([
    ['laptop', 'Laptop'],
    ['m-bag', 'M-bag'],
    ['orange ball', 'Orange ball'],
    ['Orange ball', 'Orange ball'],
    ['iPad', 'iPad'],
    ['CTB half', 'CTB half'],
    ['', ''],
  ])('sentenceCase(%j) = %j', (raw, want) => {
    expect(sentenceCase(raw)).toBe(want);
  });

  test('displayLabel is sentence case for objects; persons and fallbacks are unchanged', () => {
    expect(displayLabel({ entity_id: LAPTOP, label: 'laptop' })).toBe('Laptop');
    expect(displayLabel({ entity_id: LAPTOP, label: null })).toBe('item e6f810c9');
    expect(displayLabel({ entity_id: P1, kind: 'person', label: 'person', description: 'blue shirt' })).toBe('Person: blue shirt');
  });

  test('inventory rows, detail h1 and overview rows show "Laptop"; the raw label is the tooltip', async () => {
    itemsAre([{ ...drill, entity_id: LAPTOP, label: 'laptop' }]);
    renderApp('/');
    const row = await findItem(LAPTOP);
    const link = within(row).getByRole('link', { name: 'Laptop' });
    expect(link).toHaveAttribute('title', 'laptop');
  });

  test('detail h1 is sentence case', async () => {
    detailIs({ ...drill, entity_id: LAPTOP, label: 'm-bag', events: [], sightings: [] });
    renderApp(`/items/${LAPTOP}`);
    expect(await screen.findByRole('heading', { level: 1 })).toHaveTextContent(/^M-bag$/);
  });

  test('overview object rows are sentence case', async () => {
    server.use(http.get('*/api/events', () => HttpResponse.json([{ event_id: 'u1', event_type: 'used', entity_id: LAPTOP,
      entity_label: 'laptop', actor_desc: 'grey t-shirt', from_location: 'NOD1', from_location_name: 'Node 1 Unity',
      location_role: 'at', occurred_at: '2026-10-18T06:41:00Z', source: 'pipeline' }])));
    renderApp('/overview');
    const feed = await screen.findByRole('list', { name: /recent events/i });
    const link = await within(feed).findByRole('link');
    expect(link.textContent).toMatch(/^Laptop e6f810c9/);
    expect(link).toHaveAttribute('title', 'laptop');
  });
});

// ---------------------------------------------------------------- UI5-01 / UI5-02
const unk = (n: number, extra: Partial<ReviewItem> = {}): ReviewItem => ({
  ...REVIEWS[0], review_id: `0000000${n}-aaaa-4aaa-8aaa-aaaaaaaaaaaa`, entity_id: `0000000${n}-bbbb-4bbb-8bbb-bbbbbbbbbbbb`,
  image_url: '/media/frames/063303.jpg', image_bbox: null, ...extra,
});
const grey = unk(1, { description: 'grey object: large smooth grey object in the foreground', model_guess: 'grey object' });
const orange = unk(2, { description: 'orange bag: a small orange bag on the wall', model_guess: 'bag' });
const blue = unk(3, { description: 'blue container: a blue box', model_guess: null });
const other = unk(4, { description: 'treadmill: exercise equipment', model_guess: 'treadmill', image_url: '/media/frames/063503.jpg' });
const boxed = unk(5, { description: 'orange tool: a tool', model_guess: 'tool', image_url: '/media/frames/062808.jpg',
  image_bbox: [0.1, 0.1, 0.3, 0.3], image_bbox_source: 'detector' });

function reviewsAre(items: ReviewItem[], operator = 'ui-review-5') {
  if (operator) window.localStorage.setItem('nasa-omni.operator', operator);
  else window.localStorage.removeItem('nasa-omni.operator');
  server.use(http.get('*/api/review', () => HttpResponse.json(items)));
}

describe('UI5-01 boxless unknowns and shared frames', () => {
  test('no box: "No box: look for <label> in the frame" and nothing highlighted', async () => {
    reviewsAre([grey, boxed]);
    renderApp('/review?kind=unknown_object');
    const card = await findReviewCard(grey.review_id);
    expect(card).toHaveTextContent('No box: look for grey object in the frame');
    expect(card.querySelector('[data-testid="bbox"]')).toBeNull();
    expect(card.textContent).not.toMatch(/no box for this object/);
  });

  test('the label falls back to model_guess, then the description', async () => {
    reviewsAre([unk(6, { description: 'a long description with no label prefix at all', model_guess: 'cable' }),
      unk(7, { description: 'small widget', model_guess: null, image_url: '/media/frames/x.jpg' })]);
    renderApp('/review?kind=unknown_object');
    expect(await findReviewCard(unk(6).review_id)).toHaveTextContent('No box: look for cable in the frame');
    expect(await findReviewCard(unk(7).review_id)).toHaveTextContent('No box: look for small widget in the frame');
  });

  test('the lightbox repeats the hint', async () => {
    reviewsAre([grey]);
    const { user } = renderApp('/review?kind=unknown_object');
    const card = await findReviewCard(grey.review_id);
    await user.click(within(card).getByRole('button', { name: /enlarge image/i }));
    expect(await screen.findByRole('dialog')).toHaveTextContent(/No box: look for grey object in the frame/);
  });

  test('a boxed unknown keeps its box caption', async () => {
    reviewsAre([boxed]);
    renderApp('/review?kind=unknown_object');
    const card = await findReviewCard(boxed.review_id);
    expect(card.querySelector('[data-testid="bbox"]')).not.toBeNull();
    expect(card.textContent).not.toMatch(/No box: look for/);
  });

  test('"Same frame as 2 other unknowns" names the others; a card with its own frame has no note', async () => {
    reviewsAre([grey, orange, blue, other]);
    renderApp('/review?kind=unknown_object');
    const g = await findReviewCard(grey.review_id);
    expect(g).toHaveTextContent(/Same frame as 2 other unknowns \(orange bag, blue container\)/);
    expect(await findReviewCard(orange.review_id)).toHaveTextContent(/Same frame as 2 other unknowns \(grey object, blue container\)/);
    expect((await findReviewCard(other.review_id)).textContent).not.toMatch(/Same frame/);
  });

  test('one other: singular wording', async () => {
    reviewsAre([grey, orange]);
    renderApp('/review?kind=unknown_object');
    expect(await findReviewCard(grey.review_id)).toHaveTextContent(/Same frame as 1 other unknown \(orange bag\)/);
  });
});

describe('UI5-02 "Not inventory" bulk action on the unknown tab', () => {
  test('each unknown card has a labelled checkbox; other tabs have none', async () => {
    reviewsAre([grey, orange, other]);
    renderApp('/review?kind=unknown_object');
    const card = await findReviewCard(grey.review_id);
    expect(within(card).getByRole('checkbox', { name: /select grey object .* for bulk action/i })).not.toBeChecked();
  });

  test('no checkboxes outside the unknown tab', async () => {
    reviewsAre([grey, REVIEWS[1]]);
    renderApp('/review');
    await findReviewCard(grey.review_id);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
  });

  test('selecting shows the count; the first click opens a confirm step and posts nothing', async () => {
    reviewsAre([grey, orange, other]);
    const { user } = renderApp('/review?kind=unknown_object');
    await findReviewCard(grey.review_id);
    const bar = screen.getByRole('group', { name: /bulk actions/i });
    const btn = within(bar).getByRole('button', { name: /not inventory/i });
    expect(btn).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /select grey object/i }));
    await user.click(screen.getByRole('checkbox', { name: /select treadmill/i }));
    expect(bar).toHaveTextContent(/2 selected/);
    await user.click(btn);
    const confirm = within(bar).getByRole('group', { name: /confirm not inventory for 2 items/i });
    expect(confirm).toHaveTextContent(/grey object/);
    expect(confirm).toHaveTextContent(/treadmill/);
    expect(requestsTo('POST', /\/api\/review\/.*\/resolve/)).toHaveLength(0);
    expect(within(confirm).getByRole('button', { name: /confirm: not inventory \(2\)/i })).toHaveFocus();
  });

  test('confirming dismisses each selected review with note "not_inventory" and the operator', async () => {
    reviewsAre([grey, orange, other]);
    const { user } = renderApp('/review?kind=unknown_object');
    await findReviewCard(grey.review_id);
    await user.click(screen.getByRole('checkbox', { name: /select grey object/i }));
    await user.click(screen.getByRole('checkbox', { name: /select treadmill/i }));
    await user.click(screen.getByRole('button', { name: /^not inventory \(2 selected\)/i }));
    await user.click(screen.getByRole('button', { name: /confirm: not inventory \(2\)/i }));
    for (const r of [grey, other]) {
      await waitFor(() => expect(requestsTo('POST', `/api/review/${r.review_id}/resolve`)).toHaveLength(1));
      expect(requestsTo('POST', `/api/review/${r.review_id}/resolve`)[0].body).toEqual({
        action: 'dismiss', note: 'not_inventory', operator: 'ui-review-5',
      });
    }
    expect(requestsTo('POST', `/api/review/${orange.review_id}/resolve`)).toHaveLength(0);
    expect(await findToast(/Marked 2 items as station structure/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId(`review-${grey.review_id}`)).toBeNull());
    expect(screen.queryByTestId(`review-${other.review_id}`)).toBeNull();
    expect(screen.getByTestId(`review-${orange.review_id}`)).toBeInTheDocument();
  });

  test('a failure keeps that card and says how many failed', async () => {
    reviewsAre([grey, orange]);
    server.use(http.post(`*/api/review/${orange.review_id}/resolve`, () => HttpResponse.json({ detail: 'boom' }, { status: 500 })));
    const { user } = renderApp('/review?kind=unknown_object');
    await findReviewCard(grey.review_id);
    await user.click(screen.getByRole('checkbox', { name: /select all/i }));
    await user.click(screen.getByRole('button', { name: /^not inventory \(2 selected\)/i }));
    await user.click(screen.getByRole('button', { name: /confirm: not inventory \(2\)/i }));
    expect(await findToast(/1 of 2 could not be marked/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByTestId(`review-${grey.review_id}`)).toBeNull());
    expect(screen.getByTestId(`review-${orange.review_id}`)).toBeInTheDocument();
  });

  test('"Select all" toggles every shown unknown', async () => {
    reviewsAre([grey, orange, other]);
    const { user } = renderApp('/review?kind=unknown_object');
    await findReviewCard(grey.review_id);
    const all = screen.getByRole('checkbox', { name: /select all/i });
    await user.click(all);
    expect(screen.getByRole('group', { name: /bulk actions/i })).toHaveTextContent(/3 selected/);
    await user.click(all);
    expect(screen.getByRole('group', { name: /bulk actions/i })).toHaveTextContent(/0 selected/);
  });

  test('needs an operator', async () => {
    reviewsAre([grey], '');
    const { user } = renderApp('/review?kind=unknown_object');
    await findReviewCard(grey.review_id);
    await user.click(screen.getByRole('checkbox', { name: /select grey object/i }));
    expect(screen.getByRole('button', { name: /^not inventory \(1 selected\)/i })).toBeDisabled();
  });

  test('keyboard: Space selects, Enter opens the confirm, Escape cancels and returns focus', async () => {
    reviewsAre([grey, orange]);
    const { user } = renderApp('/review?kind=unknown_object');
    await findReviewCard(grey.review_id);
    const cb = screen.getByRole('checkbox', { name: /select grey object/i });
    cb.focus();
    await user.keyboard(' ');
    expect(cb).toBeChecked();
    const btn = screen.getByRole('button', { name: /^not inventory \(1 selected\)/i });
    btn.focus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('button', { name: /confirm: not inventory \(1\)/i })).toHaveFocus();
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('group', { name: /confirm not inventory for/i })).toBeNull();
    expect(btn).toHaveFocus();
    expect(requestsTo('POST', /\/api\/review\/.*\/resolve/)).toHaveLength(0);
  });
});
