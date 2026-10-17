// AC-UI-3 Sorting (last seen newest-first default, label, confidence) + pagination (50/page) with total shown.
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import { makeItems } from './fixtures';
import { lastItemsQuery, requestsTo, server } from './server';
import { findItem, renderApp, searchParams } from './render';
import { ID } from './fixtures';

function pagedHandler(total: number) {
  return http.get('*/api/items', ({ request }) => {
    const u = new URL(request.url);
    const offset = Number(u.searchParams.get('offset') ?? 0);
    const limit = Number(u.searchParams.get('limit') ?? 50);
    return HttpResponse.json({ items: makeItems(Math.max(0, Math.min(limit, total - offset)), offset), total });
  });
}

describe('AC-UI-3 sorting', () => {
  test('AC-UI-3 default request sorts by last seen, newest first, 50 per page from offset 0', async () => {
    renderApp('/');
    await findItem(ID.drill);
    const q = requestsTo('GET', '/api/items')[0].url.searchParams;
    expect(q.get('sort') ?? '-last_seen_at').toBe('-last_seen_at');
    expect(q.get('limit')).toBe('50');
    expect(q.get('offset') ?? '0').toBe('0');
    expect(screen.getByLabelText(/sort/i)).toHaveValue('-last_seen_at');
  });

  test('AC-UI-3 sort control offers last seen, label and confidence in both directions', async () => {
    renderApp('/');
    await findItem(ID.drill);
    const sel = screen.getByLabelText(/sort/i) as HTMLSelectElement;
    const values = Array.from(sel.options).map((o) => o.value);
    for (const v of ['-last_seen_at', 'last_seen_at', 'label', '-label', 'confidence', '-confidence']) {
      expect(values).toContain(v);
    }
  });

  test.each(['label', '-label', '-confidence', 'confidence', 'last_seen_at'])(
    'AC-UI-3 choosing sort %s sends sort= and puts it in the URL',
    async (v) => {
      const { user } = renderApp('/');
      await findItem(ID.drill);
      await user.selectOptions(screen.getByLabelText(/sort/i), v);
      await waitFor(() => expect(lastItemsQuery()?.get('sort')).toBe(v));
      expect(searchParams().get('sort')).toBe(v);
    },
  );

  test('AC-UI-3 sort from URL is applied on load', async () => {
    renderApp('/?sort=-confidence');
    await waitFor(() => expect(requestsTo('GET', '/api/items')[0]?.url.searchParams.get('sort')).toBe('-confidence'));
    expect(screen.getByLabelText(/sort/i)).toHaveValue('-confidence');
  });

  test('AC-UI-3 rows keep server order (no client-side re-sorting)', async () => {
    renderApp('/');
    const ids = [ID.drill, ID.crew, ID.camera, ID.bag];
    for (const id of ids) await findItem(id);
    const order = screen.getAllByTestId(/^item-/).map((el) => el.getAttribute('data-testid'));
    expect(order).toEqual(ids.map((id) => `item-${id}`));
  });
});

describe('AC-UI-3 pagination', () => {
  test('AC-UI-3 total count is shown', async () => {
    server.use(pagedHandler(120));
    renderApp('/');
    expect(await screen.findByText(/\b120\b.*items?|items?.*\b120\b/i)).toBeInTheDocument();
  });

  test('AC-UI-3 shows 50 rows per page and page position', async () => {
    server.use(pagedHandler(120));
    renderApp('/');
    await screen.findByText('Bulk item 1');
    expect(screen.getAllByTestId(/^item-/)).toHaveLength(50);
    expect(screen.getByText(/page 1 of 3/i)).toBeInTheDocument();
  });

  test('AC-UI-3 next page requests offset=50 and is reflected in the URL', async () => {
    server.use(pagedHandler(120));
    const { user } = renderApp('/');
    await screen.findByText('Bulk item 1');
    expect(screen.getByRole('button', { name: /previous/i })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /next/i }));
    await waitFor(() => expect(lastItemsQuery()?.get('offset')).toBe('50'));
    expect(lastItemsQuery()?.get('limit')).toBe('50');
    expect(await screen.findByText('Bulk item 51')).toBeInTheDocument();
    expect(searchParams().get('offset')).toBe('50');
    expect(screen.getByText(/page 2 of 3/i)).toBeInTheDocument();
  });

  test('AC-UI-3 last page disables next; previous goes back', async () => {
    server.use(pagedHandler(120));
    const { user } = renderApp('/?offset=100');
    expect(await screen.findByText('Bulk item 101')).toBeInTheDocument();
    expect(screen.getAllByTestId(/^item-/)).toHaveLength(20);
    expect(screen.getByRole('button', { name: /next/i })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: /previous/i }));
    await waitFor(() => expect(lastItemsQuery()?.get('offset')).toBe('50'));
  });

  test('AC-UI-3 exactly 50 results -> single page, next disabled', async () => {
    server.use(pagedHandler(50));
    renderApp('/');
    await screen.findByText('Bulk item 1');
    expect(screen.getByRole('button', { name: /next/i })).toBeDisabled();
    expect(screen.getByText(/page 1 of 1/i)).toBeInTheDocument();
  });

  test('AC-UI-3 zero results -> total 0 shown, both buttons disabled', async () => {
    server.use(pagedHandler(0));
    renderApp('/');
    expect(await screen.findByText(/\b0\b.*items?|no items/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /next/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: /previous/i })).toBeDisabled();
  });

  test('AC-UI-3 changing sort keeps filters but resets to the first page', async () => {
    server.use(pagedHandler(120));
    const { user } = renderApp('/?offset=50&kind=object');
    await screen.findByText('Bulk item 51');
    await user.selectOptions(screen.getByLabelText(/sort/i), 'label');
    await waitFor(() => expect(lastItemsQuery()?.get('sort')).toBe('label'));
    expect(lastItemsQuery()?.get('offset') ?? '0').toBe('0');
    expect(lastItemsQuery()?.get('kind')).toBe('object');
  });
});
