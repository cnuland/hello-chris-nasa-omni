// AC-UI-1 Inventory view (default route "/").
import { screen, within, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import { ID, NOW, camera, drill } from './fixtures';
import { lastItemsQuery, server } from './server';
import { findItem, freezeDate, renderApp } from './render';

describe('AC-UI-1 inventory view', () => {
  test('AC-UI-1 default route "/" lists items from GET /api/items, one container per entity', async () => {
    renderApp('/');
    for (const id of [ID.drill, ID.crew, ID.camera, ID.bag]) {
      expect(await findItem(id)).toBeInTheDocument();
    }
    expect(lastItemsQuery()).toBeDefined();
  });

  test('AC-UI-1 row shows snapshot thumbnail served through the /api proxy', async () => {
    renderApp('/');
    const row = await findItem(ID.drill);
    const img = within(row).getByRole('img', { name: /pistol-grip drill/i });
    expect(img).toHaveAttribute('src', '/api/media/snap/drill.jpg');
  });

  test('AC-UI-1 item without snapshot renders no broken <img> (placeholder instead)', async () => {
    renderApp('/');
    const row = await findItem(ID.camera);
    expect(within(row).getByText(camera.label)).toBeInTheDocument();
    for (const img of within(row).queryAllByRole('img')) {
      const src = img.getAttribute('src') ?? '';
      expect(src).not.toMatch(/null|undefined/);
      expect(src).not.toBe('');
    }
  });

  test('AC-UI-1 row shows label, catalog name and status badge', async () => {
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(within(row).getByText('Pistol-grip drill')).toBeInTheDocument();
    expect(within(row).getByText('Drill, pistol grip')).toBeInTheDocument();
    expect(within(row).getByText(/^confirmed$/i)).toBeInTheDocument();
    const row2 = await findItem(ID.camera);
    expect(within(row2).getByText(/^provisional$/i)).toBeInTheDocument();
  });

  test('AC-UI-1 row shows a confidence bar (progressbar, percent 0-100)', async () => {
    renderApp('/');
    const row = await findItem(ID.drill);
    const bar = within(row).getByRole('progressbar', { name: /confidence/i });
    expect(bar).toHaveAttribute('aria-valuenow', '87');
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
    const low = within(await findItem(ID.camera)).getByRole('progressbar', { name: /confidence/i });
    expect(low).toHaveAttribute('aria-valuenow', '42');
  });

  test('AC-UI-1 row shows last location display name and code', async () => {
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(within(row).getByText(/Lab starboard rack 2/)).toBeInTheDocument();
    expect(within(row).getByText(/LAB1S2/)).toBeInTheDocument();
  });

  test('AC-UI-1 last seen is relative, with the UTC timestamp on hover (title)', async () => {
    freezeDate(NOW);
    renderApp('/');
    const row = await findItem(ID.drill);
    const rel = within(row).getByText(/5\s*(m|min|mins|minutes?)\s+ago/i);
    expect(rel.getAttribute('title') ?? rel.closest('[title]')?.getAttribute('title')).toMatch(
      /2026-10-17[ T]12:00(:00)?(\.000)?\s*(Z|UTC)/,
    );
    // ~3 hours ago for the camera, ~1 day for the bag
    expect(within(await findItem(ID.camera)).getByText(/3\s*(h|hr|hrs|hours?)\s+ago/i)).toBeInTheDocument();
    expect(within(await findItem(ID.bag)).getByText(/(1\s*(d|day)\s+ago|yesterday)/i)).toBeInTheDocument();
  });

  test('AC-UI-1 row shows last moved by', async () => {
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(within(row).getByText(/crew member, blue shirt/)).toBeInTheDocument();
  });

  test('AC-UI-1 needs-review flag only on items with needs_review=true', async () => {
    renderApp('/');
    const flagged = await findItem(ID.camera);
    expect(
      within(flagged).queryByText(/needs review/i) ?? within(flagged).queryByLabelText(/needs review/i),
    ).not.toBeNull();
    const clean = await findItem(ID.drill);
    expect(within(clean).queryByText(/needs review/i)).toBeNull();
    expect(within(clean).queryByLabelText(/needs review/i)).toBeNull();
  });

  test('AC-UI-1 null optional fields (catalog, moved by, location) do not crash or print "null"', async () => {
    server.use(
      http.get('*/api/items', () =>
        HttpResponse.json({
          items: [{ ...drill, catalog_id: null, catalog_name: null, last_moved_by: null, last_location: null,
            last_location_name: null, last_seen_at: null, snapshot_url: null }],
          total: 1,
        }),
      ),
    );
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(row.textContent).not.toMatch(/null|undefined|NaN|Invalid Date/);
  });

  test('AC-UI-1 empty inventory shows an empty state', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.json({ items: [], total: 0 })));
    renderApp('/');
    expect(await screen.findByText(/no items/i)).toBeInTheDocument();
  });

  test('AC-UI-1 clicking an item label opens its detail (/items/:id)', async () => {
    const { user } = renderApp('/');
    const row = await findItem(ID.drill);
    await user.click(within(row).getByRole('link', { name: /pistol-grip drill/i }));
    await waitFor(() => expect(screen.getByRole('heading', { name: /pistol-grip drill/i })).toBeInTheDocument());
  });
});
