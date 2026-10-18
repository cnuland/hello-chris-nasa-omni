// AC-UI-7 Unknowns / Review queue (/review): cards (image, description, timestamp, location, model guess, kind);
// actions classify (searchable catalog select), new catalog item, merge into a candidate (side-by-side images),
// dismiss; the card disappears on success; the open-count badge shows in the nav.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { act } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { ID, NOW, REVIEWS, STATS } from './fixtures';
import { requestsTo, server } from './server';
import { fakeTimers, findItem, findReviewCard, findToast, renderApp } from './render';

const resolvePath = (id: string) => `/api/review/${id}/resolve`;

/** AC-UI-15: Dismiss is two-step (Dismiss… then Confirm dismiss). */
async function dismissCard(user: ReturnType<typeof renderApp>['user'], card: HTMLElement) {
  await user.click(within(card).getByRole('button', { name: /^dismiss/i }));
  await user.click(within(card).getByRole('button', { name: /confirm dismiss/i }));
}

async function openReview(operator = 'alice') {
  const r = renderApp('/review');
  await findReviewCard(ID.rvUnknown);
  if (operator) await r.user.type(screen.getByLabelText(/operator/i), operator);
  return r;
}

function navReviewLink() {
  return within(screen.getByRole('navigation')).getByRole('link', { name: /review|unknowns/i });
}

describe('AC-UI-7 review queue cards', () => {
  test('AC-UI-7 loads open review items (GET /api/review?status=open or default) and renders a card per item', async () => {
    renderApp('/review');
    for (const r of REVIEWS) expect(await findReviewCard(r.review_id)).toBeInTheDocument();
    const q = requestsTo('GET', '/api/review')[0].url.searchParams;
    expect(q.get('status') ?? 'open').toBe('open');
  });

  test('AC-UI-7 card shows image, description, timestamp, location, model guess and kind', async () => {
    renderApp('/review');
    const card = await findReviewCard(ID.rvUnknown);
    const img = within(card).getAllByRole('img')[0];
    expect(img).toHaveAttribute('src', '/api/media/reviews/a.jpg');
    expect(img.getAttribute('alt')).toBeTruthy();
    expect(within(card).getByText(/Small yellow cylinder with a red cap/)).toBeInTheDocument();
    expect(within(card).getByText(/2026-10-17[ T]10:15/)).toBeInTheDocument();
    expect(within(card).getByText(/NOD2D4|Node 2 deck bay 4/)).toBeInTheDocument();
    // Amended (UI5-01): the no-box hint also names the guess ("No box: look for flashlight in the frame").
    expect(within(card).getByText(/^flashlight$/i)).toBeInTheDocument();
    expect(within(card).getByText(/unknown[ _]object/i)).toBeInTheDocument();
  });

  test('AC-UI-7 empty queue shows an empty state', async () => {
    server.use(http.get('*/api/review', () => HttpResponse.json([])));
    renderApp('/review');
    expect(await screen.findByText(/no (open )?(review items|unknowns)|queue is empty|all clear/i)).toBeInTheDocument();
  });
});

describe('AC-UI-7 classify as catalog item (searchable select)', () => {
  test('AC-UI-7 typing in the catalog search filters the catalog options', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    const select = within(card).getByLabelText(/^catalog( item)?$/i) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThanOrEqual(4));
    await user.type(within(card).getByLabelText(/search catalog/i), 'drill');
    await waitFor(() => {
      const labels = Array.from(select.options).filter((o) => o.value).map((o) => o.textContent);
      expect(labels).toEqual(['Drill, pistol grip']);
    });
  });

  test('AC-UI-7 catalog search also matches class names / ids (e.g. "cargo")', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    const select = within(card).getByLabelText(/^catalog( item)?$/i) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThanOrEqual(4));
    await user.type(within(card).getByLabelText(/search catalog/i), 'cargo');
    await waitFor(() =>
      expect(Array.from(select.options).filter((o) => o.value).map((o) => o.value)).toEqual(['ctb-half']),
    );
  });

  test('AC-UI-7 classify posts assign_catalog with operator, and the card disappears', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    const select = within(card).getByLabelText(/^catalog( item)?$/i) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThanOrEqual(4));
    await user.selectOptions(select, 'drill-pistol-grip');
    await user.click(within(card).getByRole('button', { name: /classify/i }));
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvUnknown))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvUnknown))[0].body).toMatchObject({
      action: 'assign_catalog', catalog_id: 'drill-pistol-grip', operator: 'alice',
    });
    await waitFor(() => expect(screen.queryByTestId(`review-${ID.rvUnknown}`)).toBeNull());
    expect(screen.getByTestId(`review-${ID.rvDup}`)).toBeInTheDocument();
  });

  test('AC-UI-7 classify without choosing a catalog item does not post', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    const btn = within(card).getByRole('button', { name: /classify/i }) as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', resolvePath(ID.rvUnknown))).toHaveLength(0);
  });

  test('AC-UI-7 actions require an operator name (API requires operator)', async () => {
    const { user } = await openReview('');
    const card = await findReviewCard(ID.rvUnknown);
    const btn = within(card).getByRole('button', { name: /^dismiss/i }) as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', resolvePath(ID.rvUnknown))).toHaveLength(0);
    expect(screen.getByText(/operator (name )?is required|enter (your|an) operator/i)).toBeInTheDocument();
  });

  test('AC-UI-7 409 (already resolved by someone else) removes the card and shows an info toast', async () => {
    server.use(
      http.post(`*/api/review/${ID.rvUnknown}/resolve`, () =>
        HttpResponse.json({ detail: 'already resolved' }, { status: 409 }),
      ),
    );
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    await dismissCard(user, card);
    const toast = await findToast(/already resolved/i);
    expect(toast.closest('[role="alert"]')).toBeNull(); // info, not an error
    await waitFor(() => expect(screen.queryByTestId(`review-${ID.rvUnknown}`)).toBeNull());
    expect(screen.getByTestId(`review-${ID.rvDup}`)).toBeInTheDocument();
  });

  test('AC-UI-7 other failures (422/5xx) keep the card and show an error toast', async () => {
    server.use(
      http.post(`*/api/review/${ID.rvUnknown}/resolve`, () =>
        HttpResponse.json({ detail: 'invalid action payload' }, { status: 422 }),
      ),
    );
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    await dismissCard(user, card);
    expect(await findToast(/invalid action payload/i)).toBeInTheDocument();
    expect(screen.getByTestId(`review-${ID.rvUnknown}`)).toBeInTheDocument();
    expect(within(card).getByRole('button', { name: /confirm dismiss/i })).toBeEnabled();
  });
});

describe('AC-UI-7 create a new catalog item', () => {
  test('AC-UI-7 new catalog item posts new_catalog with id, class and display names', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    await user.click(within(card).getByRole('button', { name: /new catalog item/i }));
    await user.type(within(card).getByLabelText(/catalog id/i), 'flashlight-yellow');
    await user.type(within(card).getByLabelText(/class name/i), 'flashlight');
    await user.type(within(card).getByLabelText(/display name/i), 'Flashlight, yellow');
    await user.click(within(card).getByRole('button', { name: /create/i }));
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvUnknown))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvUnknown))[0].body).toMatchObject({
      action: 'new_catalog',
      operator: 'alice',
      new_catalog: { catalog_id: 'flashlight-yellow', class_name: 'flashlight', display_name: 'Flashlight, yellow' },
    });
    await waitFor(() => expect(screen.queryByTestId(`review-${ID.rvUnknown}`)).toBeNull());
  });

  test('AC-UI-7 invalid catalog id (contract pattern ^[a-z0-9][a-z0-9-]{1,63}$) is rejected client-side', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvUnknown);
    await user.click(within(card).getByRole('button', { name: /new catalog item/i }));
    await user.type(within(card).getByLabelText(/catalog id/i), 'Bad ID!');
    await user.type(within(card).getByLabelText(/class name/i), 'flashlight');
    await user.type(within(card).getByLabelText(/display name/i), 'Flashlight');
    const btn = within(card).getByRole('button', { name: /create/i }) as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', resolvePath(ID.rvUnknown))).toHaveLength(0);
    expect(within(card).getByText(/lowercase|invalid (catalog )?id/i)).toBeInTheDocument();
  });
});

describe('AC-UI-7 merge into a candidate', () => {
  test('AC-UI-7 candidates are shown side-by-side with the review image', async () => {
    renderApp('/review');
    const card = await findReviewCard(ID.rvDup);
    const srcs = within(card).getAllByRole('img').map((i) => i.getAttribute('src'));
    expect(srcs).toEqual(expect.arrayContaining(['/api/media/reviews/b.jpg', '/api/media/snap/drill.jpg', '/api/media/snap/camera.jpg']));
    const cands = within(card).getByRole('list', { name: /candidates/i });
    expect(within(cands).getAllByRole('listitem')).toHaveLength(2);
    expect(within(cands).getByText('Pistol-grip drill')).toBeInTheDocument();
    expect(within(cands).getByText('DSLR camera')).toBeInTheDocument();
  });

  test('AC-UI-7 merge posts merge_into with the chosen candidate and removes the card', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvDup);
    await user.click(within(card).getByRole('button', { name: /merge subject into this: pistol-grip drill/i }));
    // Amended (UI2-05): merge goes through a confirm step.
    await user.click(within(card).getByRole('button', { name: /confirm merge/i }));
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvDup))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvDup))[0].body).toMatchObject({
      action: 'merge_into', target_entity_id: ID.drill, operator: 'alice',
    });
    await waitFor(() => expect(screen.queryByTestId(`review-${ID.rvDup}`)).toBeNull());
  });

  test('AC-UI-7 cards without candidates offer no merge button', async () => {
    renderApp('/review');
    const card = await findReviewCard(ID.rvUnknown);
    expect(within(card).queryByRole('button', { name: /merge/i })).toBeNull();
  });
});

describe('AC-UI-7 dismiss', () => {
  test('AC-UI-7 dismiss posts action=dismiss and removes the card', async () => {
    const { user } = await openReview();
    const card = await findReviewCard(ID.rvLow);
    await dismissCard(user, card);
    await waitFor(() => expect(requestsTo('POST', resolvePath(ID.rvLow))).toHaveLength(1));
    expect(requestsTo('POST', resolvePath(ID.rvLow))[0].body).toMatchObject({ action: 'dismiss', operator: 'alice' });
    await waitFor(() => expect(screen.queryByTestId(`review-${ID.rvLow}`)).toBeNull());
  });
});

describe('AC-UI-7 nav badge', () => {
  test('AC-UI-7 the nav shows the open review count on every page', async () => {
    renderApp('/');
    await findItem(ID.drill);
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b3\b/));
  });

  test('AC-UI-7 the badge decrements after a card is resolved', async () => {
    let open = [...REVIEWS];
    server.use(
      http.get('*/api/review', () => HttpResponse.json(open)),
      http.post('*/api/review/:id/resolve', ({ params }) => {
        open = open.filter((r) => r.review_id !== params.id);
        return HttpResponse.json({ status: 'resolved' });
      }),
      http.get('*/api/stats', async () => {
        const { STATS } = await import('./fixtures');
        return HttpResponse.json({ ...STATS, open_reviews_by_kind: { unknown_object: open.length, possible_duplicate: 0, low_confidence: 0, conflict: 0 } });
      }),
    );
    const { user } = await openReview();
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b3\b/));
    await dismissCard(user, await findReviewCard(ID.rvLow));
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b2\b/));
  });
});

describe('AC-UI-7 queue completeness and badge freshness (review round 1)', () => {
  const many = Array.from({ length: 60 }, (_, i) => ({
    ...REVIEWS[0],
    review_id: `aaaaaaaa-aaaa-4aaa-8aaa-${String(i).padStart(12, '0')}`,
    description: `Unknown object #${i}`,
  }));
  /** Behaves like inventory-api: default limit 50, max 500. */
  const pagedReview = http.get('*/api/review', ({ request }) => {
    const limit = Number(new URL(request.url).searchParams.get('limit') ?? '50');
    return HttpResponse.json(many.slice(0, Math.min(limit, 500)));
  });
  const statsWithOpen = (n: number) =>
    http.get('*/api/stats', () =>
      HttpResponse.json({ ...STATS, open_reviews_by_kind: { unknown_object: n, possible_duplicate: 0, low_confidence: 0, conflict: 0 } }),
    );

  test('AC-UI-7 more than 50 open items are all reachable (limit is raised above the API default)', async () => {
    server.use(pagedReview, statsWithOpen(60));
    renderApp('/review');
    expect(await findReviewCard(many[59].review_id)).toBeInTheDocument();
    expect(screen.getAllByTestId(/^review-/)).toHaveLength(60);
    const limit = Number(requestsTo('GET', '/api/review')[0].url.searchParams.get('limit'));
    expect(limit).toBeGreaterThan(50);
    expect(limit).toBeLessThanOrEqual(500);
  });

  test('AC-UI-7 when the badge total exceeds the loaded cards the page says "showing N of M"', async () => {
    server.use(pagedReview, statsWithOpen(80));
    renderApp('/review');
    await findReviewCard(many[59].review_id);
    expect(await screen.findByText(/showing 60 of 80/i)).toBeInTheDocument();
  });

  test('AC-UI-7 opening the review page refreshes the badge', async () => {
    let open = 3;
    server.use(
      http.get('*/api/stats', () =>
        HttpResponse.json({ ...STATS, open_reviews_by_kind: { unknown_object: open, possible_duplicate: 0, low_confidence: 0, conflict: 0 } }),
      ),
    );
    const { user } = renderApp('/');
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b3\b/));
    open = 9;
    await user.click(navReviewLink());
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b9\b/));
  });

  test('AC-UI-7 the badge refreshes periodically (new unknowns appear without a reload)', async () => {
    fakeTimers(NOW);
    let open = 3;
    server.use(
      http.get('*/api/stats', () =>
        HttpResponse.json({ ...STATS, open_reviews_by_kind: { unknown_object: open, possible_duplicate: 0, low_confidence: 0, conflict: 0 } }),
      ),
    );
    renderApp('/');
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b3\b/));
    open = 5;
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    await waitFor(() => expect(navReviewLink()).toHaveTextContent(/\b5\b/));
  });
});
