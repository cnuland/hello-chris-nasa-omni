// UI review round 2 on the review queue:
//   UI2-05 merge confirm step (both thumbnails, consequence) + toast with Undo (unmerge) and a link to the target;
//   UI2-06 match_reasons chips + score, sorted by score; UI2-07 / UI-08 box styles and "whole frame" captions on the
//   review image and candidate tiles; UI2-13 "Showing x of y" when filtered; UI2-14 unique landmark names;
//   UI2-15 plain-language toasts; 1024 px: compact candidate grid and an actions footer.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { ReviewCandidate, ReviewItem } from '../src/api/types';
import { ID, REVIEWS } from './fixtures';
import { requestsTo, server } from './server';
import { current, findReviewCard, findToast, renderApp, toastRegion } from './render';

const resolvePath = (id: string) => `/api/review/${id}/resolve`;
const SUBJECT = ID.newItem;
const unmergePath = `/api/items/${SUBJECT}/unmerge`;

const cand = (id: string, extra: Partial<ReviewCandidate> = {}): ReviewCandidate => ({
  entity_id: id, label: 'headset', catalog_id: 'headset', last_location: 'LAB1S2', last_location_name: 'Lab starboard rack 2',
  last_seen_at: '2026-10-17T22:43:36Z', confidence: 0.9, thumbnail_url: `/media/thumbs/${id.slice(0, 4)}.jpg`, sighting_count: 2,
  ...extra,
});

const dup: ReviewItem = {
  ...REVIEWS[1],
  subject: {
    entity_id: SUBJECT, label: 'headset', catalog_id: 'headset', last_location: 'NOD1', last_location_name: 'Node 1 Unity',
    last_seen_at: '2026-10-17T22:56:49Z', confidence: 0.64, thumbnail_url: '/media/thumbs/subject.jpg', sighting_count: 3,
  },
  candidates: [cand(ID.drill), cand(ID.camera, { label: 'cargo bag', thumbnail_url: '/media/thumbs/target.jpg' })],
  candidates_source: 'stored',
};

function reviewsAre(items: ReviewItem[], total?: number) {
  server.use(
    http.get('*/api/review', () =>
      HttpResponse.json(items, total === undefined ? {} : { headers: { 'X-Total-Count': String(total) } }),
    ),
  );
}

async function openReview(operator = 'ui-review-2') {
  const r = renderApp('/review');
  await screen.findAllByTestId(/^review-/);
  if (operator) await r.user.type(screen.getByLabelText(/^operator$/i), operator);
  return r;
}

async function startMerge(user: Awaited<ReturnType<typeof openReview>>['user'], target = /cargo bag/i) {
  const card = await findReviewCard(ID.rvDup);
  await user.click(within(card).getAllByRole('button', { name: /merge subject into this/i }).find((b) => target.test(b.getAttribute('aria-label') ?? ''))!);
  return { card, confirm: within(card).getByRole('group', { name: /confirm merge/i }) };
}

describe('UI2-05 merge confirm step', () => {
  test('clicking merge posts nothing; the confirm shows both thumbnails and what happens', async () => {
    reviewsAre([dup]);
    const { user } = await openReview();
    const { confirm } = await startMerge(user);
    expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(0);
    const srcs = within(confirm).getAllByRole('img').map((i) => i.getAttribute('src'));
    expect(srcs).toEqual(['/api/media/thumbs/subject.jpg', '/api/media/thumbs/target.jpg']);
    expect(confirm).toHaveTextContent(/merge headset 55555555 into cargo bag 22222222/i);
    expect(confirm).toHaveTextContent(/sightings and history move to cargo bag/i);
    expect(confirm).toHaveTextContent(/undo/i);
    expect(within(confirm).getByRole('button', { name: /confirm merge/i })).toHaveFocus();
  });

  test('Cancel closes the confirm without posting', async () => {
    reviewsAre([dup]);
    const { user } = await openReview();
    const { card, confirm } = await startMerge(user);
    await user.click(within(confirm).getByRole('button', { name: /cancel/i }));
    expect(within(card).queryByRole('group', { name: /confirm merge/i })).toBeNull();
    expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(0);
  });

  test('Confirm posts merge_into; the toast names both, links to the target and offers Undo', async () => {
    reviewsAre([dup]);
    const { user } = await openReview();
    const { confirm } = await startMerge(user);
    await user.click(within(confirm).getByRole('button', { name: /confirm merge/i }));
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvDup))[0].body).toMatchObject({ action: 'merge_into', target_entity_id: ID.camera, operator: 'ui-review-2' });
    expect(await findToast(/merged headset 55555555 into cargo bag 22222222/i)).toBeInTheDocument();
    const region = toastRegion();
    expect(within(region).getByRole('link', { name: /open cargo bag/i })).toHaveAttribute('href', `/items/${ID.camera}`);
    expect(within(region).getByRole('button', { name: /^undo/i })).toBeInTheDocument();
  });

  test('Undo calls unmerge on the merged subject with the operator and reloads the queue', async () => {
    reviewsAre([dup]);
    server.use(http.post(`*/api/items/${SUBJECT}/unmerge`, () => HttpResponse.json([])));
    const { user } = await openReview();
    const { confirm } = await startMerge(user);
    await user.click(within(confirm).getByRole('button', { name: /confirm merge/i }));
    await findToast(/merged headset/i);
    const before = requestsTo('GET', '/api/review').length;
    await user.click(within(toastRegion()).getByRole('button', { name: /^undo/i }));
    await waitFor(() => expect(requestsTo('POST', unmergePath)).toHaveLength(1));
    expect(requestsTo('POST', unmergePath)[0].body).toMatchObject({ operator: 'ui-review-2' });
    expect(await findToast(/merge undone/i)).toBeInTheDocument();
    await waitFor(() => expect(requestsTo('GET', '/api/review').length).toBeGreaterThan(before));
  });

  test('Undo answered with 409 explains that the merge cannot be undone safely', async () => {
    reviewsAre([dup]);
    server.use(http.post(`*/api/items/${SUBJECT}/unmerge`, () => HttpResponse.json({ detail: 'target changed since the merge' }, { status: 409 })));
    const { user } = await openReview();
    const { confirm } = await startMerge(user);
    await user.click(within(confirm).getByRole('button', { name: /confirm merge/i }));
    await findToast(/merged headset/i);
    await user.click(within(toastRegion()).getByRole('button', { name: /^undo/i }));
    expect(await findToast(/can't be undone safely.*target changed since the merge/i)).toBeInTheDocument();
  });

  test('the toast link opens the target item', async () => {
    reviewsAre([dup]);
    const { user } = await openReview();
    const { confirm } = await startMerge(user);
    await user.click(within(confirm).getByRole('button', { name: /confirm merge/i }));
    await findToast(/merged headset/i);
    await user.click(within(toastRegion()).getByRole('link', { name: /open cargo bag/i }));
    await waitFor(() => expect(current.pathname).toBe(`/items/${ID.camera}`));
  });
});

describe('UI2-15 plain-language toasts', () => {
  test('keep both says "Kept both items", never "assign catalog"', async () => {
    reviewsAre([dup]);
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvDup);
    await user.click(within(card).getByRole('button', { name: /not a duplicate \(keep both\)/i }));
    expect(await findToast(/kept both items: headset 55555555 stays a separate item/i)).toBeInTheDocument();
    expect(toastRegion().textContent).not.toMatch(/assign catalog|review resolved/i);
  });

  test('classify and dismiss toasts use the catalog name / plain words', async () => {
    const { user } = await openReview();
    const low = await findReviewCard(ID.rvLow);
    const select = within(low).getByLabelText(/^catalog item$/i) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThanOrEqual(4));
    await user.selectOptions(select, 'camera-dslr');
    await user.click(within(low).getByRole('button', { name: /^classify$/i }));
    expect(await findToast(/classified as Camera, DSLR/i)).toBeInTheDocument();
    const unk = await findReviewCard(ID.rvUnknown);
    await user.click(within(unk).getByRole('button', { name: /dismiss/i }));
    await user.click(within(unk).getByRole('button', { name: /confirm dismiss/i }));
    expect(await findToast(/^dismissed/i)).toBeInTheDocument();
    expect(toastRegion().textContent).not.toMatch(/assign catalog|review resolved|\(dismiss\)/i);
  });
});

describe('UI2-06 match reasons and score', () => {
  test('candidates are sorted by score (highest first) and show score and reason chips', async () => {
    reviewsAre([{
      ...dup,
      candidates_source: 'suggested',
      candidates: [
        cand(ID.drill, { label: 'low', score: 0.31, match_reasons: ['same class'] }),
        cand(ID.camera, { label: 'high', score: 0.92, match_reasons: ['same module', 'similar description'] }),
        cand(ID.bag, { label: 'unscored' }),
      ],
    }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    // Direct children only: the reason chips are nested list items.
    const tiles = Array.from(within(card).getByRole('list', { name: /candidates/i }).children) as HTMLElement[];
    expect(tiles.map((t) => within(t).getAllByRole('link')[0].textContent)).toEqual(['high', 'low', 'unscored']);
    expect(tiles[0]).toHaveTextContent(/match score\s*92%/i);
    const chips = within(tiles[0]).getByRole('list', { name: /why it matched/i });
    expect(within(chips).getAllByRole('listitem').map((c) => c.textContent)).toEqual(['same module', 'similar description']);
    expect(within(card).getByText(/ranked by match score/i)).toBeInTheDocument();
  });

  test('without score or reasons: no chips, and suggested lists are labelled unranked', async () => {
    reviewsAre([{ ...dup, candidates_source: 'suggested' }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    expect(within(card).queryByRole('list', { name: /why it matched/i })).toBeNull();
    expect(within(card).getByText(/unranked/i)).toBeInTheDocument();
    expect(card.textContent).not.toMatch(/match score/i);
  });

  test('reason chips are text only and non-string reasons are dropped', async () => {
    reviewsAre([{ ...dup, candidates: [cand(ID.drill, { match_reasons: ['<b>x</b>', 7 as unknown as string] })] }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const chips = within(card).getByRole('list', { name: /why it matched/i });
    expect(within(chips).getAllByRole('listitem')).toHaveLength(1);
    expect(chips.querySelector('b')).toBeNull();
  });
});

describe('UI2-07 / UI-08 boxes on review images and candidate tiles', () => {
  test('omni review box is dashed + "approximate"; detector is solid', async () => {
    reviewsAre([
      { ...REVIEWS[0], image_bbox: [0.4, 0.4, 0.6, 0.6], image_bbox_source: 'omni' },
      { ...REVIEWS[2], image_bbox: [0.1, 0.1, 0.3, 0.3], image_bbox_source: 'detector' },
    ]);
    renderApp('/review');
    const unk = await findReviewCard(ID.rvUnknown);
    expect(within(unk).getByTestId('bbox')).toHaveClass('bbox-approx');
    expect(unk).toHaveTextContent(/approximate/i);
    const low = await findReviewCard(ID.rvLow);
    expect(within(low).getByTestId('bbox')).toHaveClass('bbox-detector');
    expect(low).toHaveTextContent(/outlined box marks the object/i);
  });

  test('no review box -> caption says "whole frame"', async () => {
    renderApp('/review');
    const unk = await findReviewCard(ID.rvUnknown);
    expect(within(unk).queryByTestId('bbox')).toBeNull();
    expect(unk).toHaveTextContent(/whole frame/i);
  });

  test('a candidate with snapshot_bbox shows its snapshot with the box; one without says whole frame', async () => {
    reviewsAre([{
      ...dup,
      candidates: [
        cand(ID.drill, { snapshot_url: '/media/snap/c1.jpg', snapshot_bbox: [0.2, 0.2, 0.6, 0.6], snapshot_bbox_source: 'detector' }),
        cand(ID.camera),
      ],
    }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const tiles = within(within(card).getByRole('list', { name: /candidates/i })).getAllByRole('listitem');
    expect(within(tiles[0]).getAllByRole('img')[0]).toHaveAttribute('src', '/api/media/snap/c1.jpg');
    expect(within(tiles[0]).getByTestId('bbox')).toHaveClass('bbox-detector');
    expect(within(tiles[1]).queryByTestId('bbox')).toBeNull();
    expect(tiles[1]).toHaveTextContent(/whole frame/i);
  });
});

describe('UI2-14 landmarks have unique names', () => {
  test('two duplicate cards produce no repeated region names', async () => {
    const second: ReviewItem = { ...dup, review_id: 'b2bbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', entity_id: ID.crew, subject: { ...dup.subject!, entity_id: ID.crew } };
    reviewsAre([dup, second]);
    renderApp('/review');
    await findReviewCard(ID.rvDup);
    await findReviewCard(second.review_id);
    const names = screen.getAllByRole('region').map((r) => r.getAttribute('aria-label') ?? document.getElementById(r.getAttribute('aria-labelledby') ?? '')?.textContent ?? '');
    expect(names.length).toBeGreaterThan(2);
    expect(new Set(names).size).toBe(names.length);
  });
});

describe('UI2-13 filtered queue count', () => {
  test('a filtered queue says "Showing x of y open review items" (server filtering)', async () => {
    reviewsAre([REVIEWS[0]], 1);
    renderApp('/review?location=NOD2&q=cyl');
    await findReviewCard(ID.rvUnknown);
    expect(await screen.findByText(/showing 1 of 3 open review items/i)).toBeInTheDocument();
    expect(screen.getByText(/tab counts are totals/i)).toBeInTheDocument();
  });

  test('client-side filtering (no X-Total-Count) also shows the count', async () => {
    renderApp('/review?location=LAB1');
    await findReviewCard(ID.rvDup);
    expect(await screen.findByText(/showing 1 of 3 open review items/i)).toBeInTheDocument();
  });

  test('unfiltered: no count line when everything is shown', async () => {
    renderApp('/review');
    await findReviewCard(ID.rvDup);
    expect(screen.queryByText(/showing \d+ of/i)).toBeNull();
  });
});

describe('1024 px: compact candidate grid and actions footer', () => {
  const many = [ID.drill, ID.camera, ID.crew, ID.bag, ID.window1, ID.rvLow, ID.rvUnknown, ID.newItem.replace('5555', '6666')]
    .map((id, i) => cand(id, { label: `cand ${i + 1}` }));

  test('with 8 candidates only 4 are shown until "Show 4 more candidates"', async () => {
    reviewsAre([{ ...dup, candidates: many }]);
    const { user } = renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const list = within(card).getByRole('list', { name: /candidates/i });
    expect(within(list).getAllByRole('listitem')).toHaveLength(4);
    await user.click(within(card).getByRole('button', { name: /show 4 more candidates/i }));
    expect(within(list).getAllByRole('listitem')).toHaveLength(8);
  });

  test('duplicate-card actions live in a footer directly under the card (sticky in CSS)', async () => {
    reviewsAre([dup]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const footer = card.querySelector(':scope > .review-footer');
    expect(footer).not.toBeNull();
    expect(within(footer as HTMLElement).getByRole('button', { name: /keep both/i })).toBeInTheDocument();
  });
});
