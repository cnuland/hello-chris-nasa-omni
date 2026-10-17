// AC-UI-4 Item detail (drawer or page at /items/:id): large snapshot, attributes, history timeline with evidence
// images, and "moved by ... at ..." lines.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import { ID } from './fixtures';
import { requestsTo, server } from './server';
import { findToast, renderApp } from './render';

const detailPath = `/items/${ID.drill}`;

async function openDetail() {
  const r = renderApp(detailPath);
  await screen.findByRole('heading', { name: /pistol-grip drill/i });
  return r;
}

describe('AC-UI-4 item detail', () => {
  test('AC-UI-4 loads GET /api/items/{id} and /api/items/{id}/history', async () => {
    await openDetail();
    await waitFor(() => {
      expect(requestsTo('GET', `/api/items/${ID.drill}`)).toHaveLength(1);
      expect(requestsTo('GET', `/api/items/${ID.drill}/history`).length).toBeGreaterThanOrEqual(1);
    });
  });

  test('AC-UI-4 shows a large snapshot via /api/media', async () => {
    await openDetail();
    const imgs = screen.getAllByRole('img', { name: /pistol-grip drill/i });
    expect(imgs.some((i) => i.getAttribute('src') === '/api/media/snap/drill.jpg')).toBe(true);
  });

  test('AC-UI-4 shows core fields and attributes', async () => {
    await openDetail();
    expect(screen.getAllByText(/Drill, pistol grip/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Orange pistol-grip power tool/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Lab starboard rack 2/).length).toBeGreaterThan(0);
    // attributes: key and value
    expect(screen.getByText(/^color$/i)).toBeInTheDocument();
    expect(screen.getByText(/^orange$/i)).toBeInTheDocument();
    expect(screen.getByText(/^battery$/i)).toBeInTheDocument();
    expect(screen.getByText(/^18V$/)).toBeInTheDocument();
  });

  test('AC-UI-4 history timeline lists sightings, events and audit entries with summaries', async () => {
    await openDetail();
    const list = await screen.findByRole('list', { name: /history|timeline/i });
    const entries = await within(list).findAllByRole('listitem');
    expect(entries).toHaveLength(3);
    expect(within(list).getByText(/Seen on starboard rack 2/)).toBeInTheDocument();
    expect(within(list).getByText(/Moved from NOD2D4 to LAB1S2/)).toBeInTheDocument();
    expect(within(list).getByText(/Label edited by alice/)).toBeInTheDocument();
    for (const k of [/sighting/i, /event/i, /audit/i]) expect(within(list).getAllByText(k).length).toBeGreaterThan(0);
  });

  test('AC-UI-4 timeline entries show evidence images (and none when image_url is null)', async () => {
    await openDetail();
    const list = await screen.findByRole('list', { name: /history|timeline/i });
    const entries = await within(list).findAllByRole('listitem');
    const srcs = entries.map((e) => within(e).queryAllByRole('img').map((i) => i.getAttribute('src')));
    expect(srcs[0]).toContain('/api/media/f/1.jpg');
    expect(srcs[1]).toContain('/api/media/ev/1.jpg');
    expect(srcs[2]).toEqual([]);
  });

  test('AC-UI-4 shows "moved by ... at ..." lines for moved events (UTC time)', async () => {
    await openDetail();
    expect(
      await screen.findByText(/moved by crew member, blue shirt at .*2026-10-17[ T]11:00/i),
    ).toBeInTheDocument();
  });

  test('AC-UI-4 moved event without actor says unknown rather than "null"', async () => {
    await openDetail();
    expect(await screen.findByText(/moved by (an )?unknown.* at .*2026-10-16[ T]09:30/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/moved by null/i);
  });

  test('AC-UI-4 unknown entity -> not-found message, no crash', async () => {
    renderApp('/items/99999999-9999-4999-8999-999999999999');
    expect(await screen.findByText(/not found/i)).toBeInTheDocument();
  });

  test('AC-UI-4 server error on detail -> toast, page still usable (nav visible)', async () => {
    server.use(http.get(`*/api/items/${ID.drill}`, () => HttpResponse.json({ detail: 'db down' }, { status: 500 })));
    renderApp(detailPath);
    expect(await findToast(/db down|error|failed/i)).toBeInTheDocument();
    expect(screen.getByRole('navigation')).toBeInTheDocument();
  });

  test('AC-UI-4 empty history shows an empty-state line', async () => {
    server.use(http.get(`*/api/items/${ID.drill}/history`, () => HttpResponse.json([])));
    await openDetail();
    expect(await screen.findByText(/no history/i)).toBeInTheDocument();
  });
});
