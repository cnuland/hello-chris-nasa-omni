// UI review fixes on the review queue:
//   AC-UI-13 duplicate merge decision (subject vs candidates side by side, keep both, explicit empty state);
//   AC-UI-14 bbox overlay + accessible lightbox; AC-UI-15 dismiss confirm; AC-UI-16 filters, sort, item links.
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { ReviewItem } from '../src/api/types';
import { applyReviewFilters } from '../src/pages/ReviewPage';
import { validBBox } from '../src/components/FrameImage';
import { ID, LOCATIONS, REVIEWS, camera } from './fixtures';
import { requestsTo, server } from './server';
import { current, findReviewCard, renderApp, searchParams } from './render';

const resolvePath = (id: string) => `/api/review/${id}/resolve`;
const SUBJECT_ID = ID.newItem;

/** A possible_duplicate review exactly as the extended inventory-api returns it (subject, candidates, bbox). */
const dupNew: ReviewItem = {
  ...REVIEWS[1],
  subject: {
    entity_id: SUBJECT_ID, label: 'headset', catalog_id: 'headset', status: 'provisional', last_location: 'NOD1',
    last_location_name: 'Node 1 Unity', last_seen_at: '2026-10-17T22:56:49Z', confidence: 0.64,
    thumbnail_url: '/media/thumbs/subject.jpg', sighting_count: 3,
  },
  candidates: [
    {
      entity_id: ID.drill, label: 'headset', catalog_id: 'headset', status: 'confirmed', last_location: 'LAB1S2',
      last_location_name: 'Lab starboard rack 2', last_seen_at: '2026-10-17T22:43:36Z', confidence: 0.91,
      thumbnail_url: '/media/thumbs/c1.jpg', sighting_count: 12, similarity: 0.87,
    },
    {
      entity_id: ID.camera, label: 'headset', catalog_id: 'headset', status: 'provisional', last_location: null,
      last_location_name: null, last_seen_at: null, confidence: 0.4, thumbnail_url: null, sighting_count: 1,
      similarity: null,
    },
  ],
  candidates_source: 'stored',
  image_bbox: [0.1, 0.2, 0.5, 0.6],
};

const unknownWithBox: ReviewItem = { ...REVIEWS[0], image_bbox: [0.25, 0.25, 0.75, 0.5] };

function reviewsAre(items: ReviewItem[], total?: number) {
  server.use(
    http.get('*/api/review', () =>
      HttpResponse.json(items, total === undefined ? {} : { headers: { 'X-Total-Count': String(total) } }),
    ),
  );
}

async function openReview(operator = 'alice') {
  const r = renderApp('/review');
  await screen.findAllByTestId(/^review-/);
  if (operator) await r.user.type(screen.getByLabelText(/^operator$/i), operator);
  return r;
}

describe('AC-UI-13 possible-duplicate cards support a merge decision', () => {
  test('AC-UI-13 subject and each candidate show thumbnail, label link, location name + code, UTC + relative, confidence, sightings', async () => {
    reviewsAre([dupNew]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const subject = within(card).getByRole('region', { name: /subject/i });
    expect(within(subject).getByRole('link', { name: 'headset' })).toHaveAttribute('href', `/items/${SUBJECT_ID}`);
    expect(within(subject).getByText('Node 1 Unity (NOD1)')).toBeInTheDocument();
    expect(within(subject).getByText('2026-10-17 22:56 UTC')).toBeInTheDocument();
    expect(within(subject).getByRole('progressbar', { name: /confidence/i })).toHaveAttribute('aria-valuenow', '64');
    expect(within(subject).getByText('3')).toBeInTheDocument();

    const cands = within(card).getByRole('list', { name: /candidates/i });
    const tiles = within(cands).getAllByRole('listitem');
    expect(tiles).toHaveLength(2);
    const first = tiles[0];
    expect(within(first).getByRole('img', { name: 'headset' })).toHaveAttribute('src', '/api/media/thumbs/c1.jpg');
    expect(within(first).getByRole('link', { name: 'headset' })).toHaveAttribute('href', `/items/${ID.drill}`);
    expect(within(first).getByText('Lab starboard rack 2 (LAB1S2)')).toBeInTheDocument();
    expect(within(first).getByText('2026-10-17 22:43 UTC')).toBeInTheDocument();
    expect(within(first).getByText(/ago|in the future/)).toBeInTheDocument();
    expect(within(first).getByText('87%')).toBeInTheDocument(); // similarity
    expect(within(first).getByText('12')).toBeInTheDocument(); // sightings
    expect(within(first).getByText(ID.drill.slice(0, 8))).toBeInTheDocument();
  });

  test('AC-UI-13 missing candidate fields degrade to dashes / placeholders (no null, NaN or Invalid Date)', async () => {
    reviewsAre([dupNew]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const second = within(within(card).getByRole('list', { name: /candidates/i })).getAllByRole('listitem')[1];
    expect(second.textContent).not.toMatch(/null|undefined|NaN|Invalid Date/);
    expect(within(second).getByText('never')).toBeInTheDocument();
  });

  test('AC-UI-13 each merge button has a unique accessible name and posts merge_into the chosen candidate', async () => {
    reviewsAre([dupNew]);
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvDup);
    const buttons = within(card).getAllByRole('button', { name: /merge subject into this/i });
    expect(buttons).toHaveLength(2);
    const names = buttons.map((b) => b.getAttribute('aria-label'));
    expect(new Set(names).size).toBe(2);
    expect(names[0]).toMatch(/headset 11111111 \(LAB1S2\)/);
    await user.click(buttons[1]);
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvDup))[0].body).toMatchObject({
      action: 'merge_into', target_entity_id: ID.camera, operator: 'alice',
    });
  });

  test('AC-UI-13 "Not a duplicate (keep both)" closes the review with assign_catalog keeping the subject label', async () => {
    reviewsAre([dupNew]);
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvDup);
    await user.click(within(card).getByRole('button', { name: /not a duplicate \(keep both\)/i }));
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvDup))[0].body).toMatchObject({
      action: 'assign_catalog', catalog_id: 'headset', label: 'headset', operator: 'alice',
    });
    await waitFor(() => expect(screen.queryByTestId(`review-${ID.rvDup}`)).toBeNull());
  });

  test('AC-UI-13 keep both without a known subject catalog waits for a catalog choice', async () => {
    reviewsAre([{ ...dupNew, subject: { ...dupNew.subject!, catalog_id: null } }]);
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvDup);
    const keep = within(card).getByRole('button', { name: /keep both/i });
    expect(keep).toBeDisabled();
    expect(keep).toHaveAccessibleDescription(/choose one/i);
    const select = within(card).getByLabelText(/^catalog item$/i) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThanOrEqual(4));
    await user.selectOptions(select, 'camera-dslr');
    expect(keep).toBeEnabled();
    await user.click(keep);
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvDup))[0].body).toMatchObject({ action: 'assign_catalog', catalog_id: 'camera-dslr' });
  });

  test('AC-UI-13 empty candidates are explicit: nothing to merge into, with a link to search the inventory', async () => {
    reviewsAre([{ ...dupNew, candidates: [], candidates_source: 'none' }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    expect(within(card).getByText(/no candidate items were found/i)).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /merge/i })).toBeNull();
    expect(within(card).getByRole('link', { name: /search the inventory for "headset"/i })).toHaveAttribute('href', '/?q=headset');
    expect(within(card).getByRole('button', { name: /keep both/i })).toBeInTheDocument();
  });

  test('AC-UI-13 server-suggested candidates are labelled as suggestions', async () => {
    reviewsAre([{ ...dupNew, candidates_source: 'suggested' }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    expect(within(card).getByText(/suggested/i)).toBeInTheDocument();
  });

  test('AC-UI-13 without `subject` (older API) the subject is loaded from GET /items/{entity_id}', async () => {
    server.use(http.get(`*/api/items/${SUBJECT_ID}`, () => HttpResponse.json({ ...camera, entity_id: SUBJECT_ID, label: 'Subject camera' })));
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const subject = within(card).getByRole('region', { name: /subject/i });
    expect(await within(subject).findByRole('link', { name: 'Subject camera' })).toHaveAttribute('href', `/items/${SUBJECT_ID}`);
    expect(within(subject).getByText('Node 2 deck bay 4 (NOD2D4)')).toBeInTheDocument();
  });

  test('AC-UI-13 a label with markup is rendered as text (no injected elements)', async () => {
    const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>';
    reviewsAre([{ ...dupNew, subject: { ...dupNew.subject!, label: evil }, description: evil }]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    expect(within(card).getAllByText(evil).length).toBeGreaterThan(0);
    expect(card.querySelector('script')).toBeNull();
    expect(card.querySelector('img[src="x"]')).toBeNull();
  });
});

describe('AC-UI-14 bbox overlay and lightbox', () => {
  test('AC-UI-14 validBBox accepts normalised boxes and rejects malformed ones', () => {
    expect(validBBox([0.1, 0.2, 0.5, 0.6])).toEqual([0.1, 0.2, 0.5, 0.6]);
    expect(validBBox([0.5, 0.5, 0.4, 0.9])).toBeNull();
    expect(validBBox([0, 0, 1])).toBeNull();
    expect(validBBox(['0', 0, 1, 1])).toBeNull();
    expect(validBBox(null)).toBeNull();
    expect(validBBox([-1, 0, 2, 0.5])).toEqual([0, 0, 1, 0.5]);
  });

  test('AC-UI-14 a review image with image_bbox draws the box at the normalised position', async () => {
    reviewsAre([unknownWithBox]);
    renderApp('/review');
    const card = await findReviewCard(ID.rvUnknown);
    const box = within(card).getByTestId('bbox');
    expect(box.style.left).toBe('25%');
    expect(box.style.top).toBe('25%');
    expect(box.style.width).toBe('50%');
    expect(box.style.height).toBe('25%');
  });

  test('AC-UI-14 no bbox -> no overlay', async () => {
    renderApp('/review');
    const card = await findReviewCard(ID.rvUnknown);
    expect(within(card).queryByTestId('bbox')).toBeNull();
  });

  test('AC-UI-14 clicking the image opens a modal dialog; focus is inside, trapped, Esc closes and returns focus', async () => {
    reviewsAre([unknownWithBox]);
    const { user } = renderApp('/review');
    const card = await findReviewCard(ID.rvUnknown);
    const trigger = within(card).getByRole('button', { name: /enlarge image/i });
    await user.click(trigger);
    const dialog = await screen.findByRole('dialog', { name: /enlarged image/i });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    const close = within(dialog).getByRole('button', { name: /close/i });
    expect(close).toHaveFocus();
    expect(within(dialog).getByRole('img')).toHaveAttribute('src', '/api/media/reviews/a.jpg');
    expect(within(dialog).getByTestId('bbox')).toBeInTheDocument();
    await user.tab();
    expect(close).toHaveFocus(); // only focusable element: Tab stays inside
    await user.tab({ shift: true });
    expect(close).toHaveFocus();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  test('AC-UI-14 the Close button and a backdrop click also close the lightbox', async () => {
    const { user } = renderApp('/review');
    const card = await findReviewCard(ID.rvUnknown);
    await user.click(within(card).getByRole('button', { name: /enlarge image/i }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: /close/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    await user.click(within(card).getByRole('button', { name: /enlarge image/i }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(dialog.parentElement!);
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });
});

describe('AC-UI-15 dismiss needs a confirm step and says what it does', () => {
  test('AC-UI-15 the first click posts nothing and explains that the item is marked dismissed', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvLow);
    await user.click(within(card).getByRole('button', { name: /^dismiss/i }));
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', resolvePath(ID.rvLow))).toHaveLength(0);
    const group = within(card).getByRole('group', { name: /confirm dismiss/i });
    expect(group).toHaveTextContent(/marks the item as dismissed/i);
    expect(group).toHaveTextContent(/hides it from the default inventory/i);
  });

  test('AC-UI-15 Cancel closes the confirm step without posting', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvLow);
    await user.click(within(card).getByRole('button', { name: /^dismiss/i }));
    await user.click(within(card).getByRole('button', { name: /cancel/i }));
    expect(within(card).queryByRole('group', { name: /confirm dismiss/i })).toBeNull();
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', resolvePath(ID.rvLow))).toHaveLength(0);
  });

  test('AC-UI-15 on a duplicate the confirm text points to "keep both"', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvDup);
    await user.click(within(card).getByRole('button', { name: /^dismiss/i }));
    expect(within(card).getByRole('group', { name: /confirm dismiss/i })).toHaveTextContent(/keep both/i);
  });

  test('AC-UI-15 a review without an entity says no inventory item is affected', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown); // entity_id: null
    await user.click(within(card).getByRole('button', { name: /^dismiss/i }));
    expect(within(card).getByRole('group', { name: /confirm dismiss/i })).toHaveTextContent(/no inventory item/i);
  });
});

describe('AC-UI-16 review queue filters, sort and item links', () => {
  test('AC-UI-16 kind tabs show counts from /stats and filter via GET /review?kind=', async () => {
    const { user } = renderApp('/review');
    await findReviewCard(ID.rvUnknown);
    const tabs = screen.getByRole('group', { name: /review kind/i });
    const dupTab = await within(tabs).findByRole('button', { name: /possible duplicates \(1\)/i });
    expect(within(tabs).getByRole('button', { name: /all \(3\)/i })).toHaveAttribute('aria-pressed', 'true');
    expect(within(tabs).queryByRole('button', { name: /conflicts/i })).toBeNull(); // 0 open: hidden
    await user.click(dupTab);
    await waitFor(() => expect(searchParams().get('kind')).toBe('possible_duplicate'));
    await waitFor(() => {
      const last = requestsTo('GET', '/api/review').at(-1)!.url.searchParams;
      expect(last.get('kind')).toBe('possible_duplicate');
      expect(last.get('status')).toBe('open');
    });
    expect(dupTab).toHaveAttribute('aria-pressed', 'true');
  });

  test('AC-UI-16 location, search (debounced) and sort map 1:1 to GET /review params and the URL', async () => {
    const { user } = renderApp('/review');
    await findReviewCard(ID.rvUnknown);
    await user.selectOptions(screen.getByLabelText(/^location$/i), 'NOD2');
    await user.type(screen.getByLabelText(/search reviews/i), 'cylinder');
    await user.selectOptions(screen.getByLabelText(/^sort$/i), 'observed_at:asc');
    await waitFor(() => {
      const last = requestsTo('GET', '/api/review').at(-1)!.url.searchParams;
      expect(last.get('location')).toBe('NOD2');
      expect(last.get('q')).toBe('cylinder');
      expect(last.get('sort')).toBe('observed_at');
      expect(last.get('order')).toBe('asc');
    });
    expect(searchParams().get('q')).toBe('cylinder');
  });

  test('AC-UI-16 filters are restored from the URL on load', async () => {
    renderApp('/review?kind=unknown_object&location=NOD2&q=cyl&sort=observed_at&order=desc');
    await waitFor(() => expect(requestsTo('GET', '/api/review').length).toBeGreaterThan(0));
    const first = requestsTo('GET', '/api/review')[0].url.searchParams;
    expect(first.get('kind')).toBe('unknown_object');
    expect(first.get('location')).toBe('NOD2');
    expect(first.get('q')).toBe('cyl');
    expect(screen.getByLabelText(/search reviews/i)).toHaveValue('cyl');
  });

  test('AC-UI-16 without X-Total-Count (older API ignoring filters) the queue is filtered client-side', async () => {
    renderApp('/review?location=LAB1');
    expect(await findReviewCard(ID.rvDup)).toBeInTheDocument(); // LAB1S2 is inside LAB1
    expect(screen.queryByTestId(`review-${ID.rvUnknown}`)).toBeNull(); // NOD2D4
    expect(screen.queryByTestId(`review-${ID.rvLow}`)).toBeNull();
  });

  test('AC-UI-16 with X-Total-Count the server result is trusted and drives "Showing N of M"', async () => {
    reviewsAre([REVIEWS[0]], 37);
    renderApp('/review?q=zzz');
    expect(await findReviewCard(ID.rvUnknown)).toBeInTheDocument();
    expect(await screen.findByText(/showing 1 of 37/i)).toBeInTheDocument();
  });

  test('AC-UI-16 a filtered empty result says no items match the filters', async () => {
    reviewsAre([], 0);
    renderApp('/review?kind=conflict');
    expect(await screen.findByText(/no open review items match these filters/i)).toBeInTheDocument();
  });

  test('AC-UI-16 applyReviewFilters: kind, location subtree, text and observed_at sort', () => {
    const out = applyReviewFilters(REVIEWS, { location: 'NOD2', sort: 'observed_at', order: 'asc' }, LOCATIONS);
    expect(out.map((r) => r.review_id)).toEqual([ID.rvLow, ID.rvUnknown]);
    expect(applyReviewFilters(REVIEWS, { q: 'ORANGE' }, LOCATIONS).map((r) => r.review_id)).toEqual([ID.rvDup]);
    expect(applyReviewFilters(REVIEWS, { kind: 'low_confidence' }, LOCATIONS).map((r) => r.review_id)).toEqual([ID.rvLow]);
  });

  test('AC-UI-16 each card with an entity links to the item page and its history', async () => {
    const { user } = renderApp('/review');
    const card = await findReviewCard(ID.rvLow);
    const open = within(card).getByRole('link', { name: /^open item/i });
    expect(open).toHaveAttribute('href', `/items/${ID.camera}`);
    expect(within(card).getByRole('link', { name: /^history/i })).toHaveAttribute('href', `/items/${ID.camera}#history`);
    expect(within(await findReviewCard(ID.rvUnknown)).queryByRole('link', { name: /^open item/i })).toBeNull();
    await user.click(open);
    await waitFor(() => expect(current.pathname).toBe(`/items/${ID.camera}`));
  });

  test('AC-UI-16 kind counts fall back gracefully when /stats fails', async () => {
    server.use(http.get('*/api/stats', () => HttpResponse.json({ detail: 'down' }, { status: 500 })));
    renderApp('/review');
    await findReviewCard(ID.rvUnknown);
    const tabs = screen.getByRole('group', { name: /review kind/i });
    expect(within(tabs).getByRole('button', { name: /^all$/i })).toBeInTheDocument();
    expect(within(tabs).getByRole('button', { name: /^unknown objects$/i })).toBeInTheDocument();
  });
});
