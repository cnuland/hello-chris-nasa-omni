// AC-UI-9 Overview (/overview): stats tiles (items, unknowns, possible duplicates, people seen in the last hour) and a
// recent events feed that auto-refreshes every 10 s.
import { act, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test, vi } from 'vitest';
import { NOW, OVERVIEW_STATS, RECENT_EVENTS } from './fixtures';
import { requestsTo, server } from './server';
import { fakeTimers, findToast, renderApp } from './render';

function useOverviewStats() {
  server.use(http.get('*/api/stats', () => HttpResponse.json(OVERVIEW_STATS)));
}

const tile = (name: RegExp) => screen.findByRole('region', { name });

describe('AC-UI-9 stats tiles', () => {
  test('AC-UI-9 Items tile = live inventory objects (by_kind.object; excludes person tracks and merged/dismissed)', async () => {
    useOverviewStats();
    renderApp('/overview');
    const t = await tile(/^items$/i);
    await waitFor(() => expect(t).toHaveTextContent(/\b37\b/));
    expect(t).not.toHaveTextContent(/\b42\b/); // live entities incl. persons
    expect(t).not.toHaveTextContent(/\b45\b/); // all entities incl. merged/dismissed
  });

  test('AC-UI-9 Unknowns tile = open unknown_object reviews', async () => {
    useOverviewStats();
    renderApp('/overview');
    const t = await tile(/unknowns/i);
    await waitFor(() => expect(t).toHaveTextContent(/\b7\b/));
  });

  test('AC-UI-9 Possible duplicates tile = open possible_duplicate reviews', async () => {
    useOverviewStats();
    renderApp('/overview');
    const t = await tile(/possible duplicates/i);
    await waitFor(() => expect(t).toHaveTextContent(/\b3\b/));
  });

  test('AC-UI-9 People tile = persons seen in the last hour (GET /items?kind=person&seen_after=now-1h)', async () => {
    fakeTimers(NOW);
    useOverviewStats();
    renderApp('/overview');
    const t = await tile(/people/i);
    await waitFor(() => expect(t).toHaveTextContent(/\b2\b/));
    const req = requestsTo('GET', '/api/items').find((r) => r.url.searchParams.get('kind') === 'person');
    expect(req).toBeDefined();
    const after = Date.parse(req!.url.searchParams.get('seen_after') ?? '');
    expect(Math.abs(after - (NOW.getTime() - 3600_000))).toBeLessThan(60_000);
  });

  test('AC-UI-9 stats failure -> toast, the events feed still renders', async () => {
    server.use(http.get('*/api/stats', () => HttpResponse.json({ detail: 'stats down' }, { status: 500 })));
    renderApp('/overview');
    expect(await findToast(/stats down|error|failed/i)).toBeInTheDocument();
    const feed = await screen.findByRole('list', { name: /recent events/i });
    expect(await within(feed).findAllByRole('listitem')).toHaveLength(RECENT_EVENTS.length);
  });
});

describe('AC-UI-9 recent events feed', () => {
  test('AC-UI-9 feed lists recent events with type, actor, from -> to and time', async () => {
    renderApp('/overview');
    const feed = await screen.findByRole('list', { name: /recent events/i });
    const items = await within(feed).findAllByRole('listitem');
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent(/moved/i);
    expect(items[0]).toHaveTextContent(/crew member, blue shirt/);
    expect(items[0]).toHaveTextContent(/NOD2D4/);
    expect(items[0]).toHaveTextContent(/LAB1S2/);
    expect(items[0]).toHaveTextContent(/11:00/);
    expect(items[1]).toHaveTextContent(/added/i);
    expect(requestsTo('GET', '/api/events').length).toBeGreaterThanOrEqual(1);
  });

  test('AC-UI-9 feed auto-refreshes every 10 s (not before)', async () => {
    fakeTimers(NOW);
    renderApp('/overview');
    await screen.findByRole('list', { name: /recent events/i });
    await waitFor(() => expect(requestsTo('GET', '/api/events')).toHaveLength(1));
    const fresh = {
      ...RECENT_EVENTS[0], event_id: 'e0000000-0000-4000-8000-000000000099', event_type: 'stowed',
      actor_desc: 'crew member, red shirt', occurred_at: '2026-10-17T12:04:00Z',
    };
    server.use(http.get('*/api/events', () => HttpResponse.json([fresh, ...RECENT_EVENTS])));
    act(() => {
      vi.advanceTimersByTime(9_000);
    });
    expect(requestsTo('GET', '/api/events')).toHaveLength(1);
    act(() => {
      vi.advanceTimersByTime(1_100);
    });
    await waitFor(() => expect(requestsTo('GET', '/api/events')).toHaveLength(2));
    const feed = screen.getByRole('list', { name: /recent events/i });
    await waitFor(() => expect(feed).toHaveTextContent(/crew member, red shirt/));
    act(() => {
      vi.advanceTimersByTime(10_000);
    });
    await waitFor(() => expect(requestsTo('GET', '/api/events')).toHaveLength(3));
  });

  test('AC-UI-9 refresh stops when leaving the overview (no leaked interval)', async () => {
    fakeTimers(NOW);
    const { unmount } = renderApp('/overview');
    await screen.findByRole('list', { name: /recent events/i });
    await waitFor(() => expect(requestsTo('GET', '/api/events')).toHaveLength(1));
    unmount();
    act(() => {
      vi.advanceTimersByTime(30_000);
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(requestsTo('GET', '/api/events')).toHaveLength(1);
  });

  test('AC-UI-9 overview is reachable from the nav', async () => {
    const { user } = renderApp('/');
    await user.click(within(screen.getByRole('navigation')).getByRole('link', { name: /overview/i }));
    expect(await screen.findByRole('list', { name: /recent events/i })).toBeInTheDocument();
  });
});

describe('AC-UI-9 polling races (review round 1)', () => {
  test('AC-UI-9 a slow older response never overwrites a newer refresh', async () => {
    fakeTimers(NOW);
    let calls = 0;
    let releaseFirst!: () => void;
    const stale = { ...RECENT_EVENTS[0], event_id: 'e0000000-0000-4000-8000-0000000000a1', actor_desc: 'STALE actor' };
    const fresh = { ...RECENT_EVENTS[0], event_id: 'e0000000-0000-4000-8000-0000000000a2', actor_desc: 'FRESH actor' };
    server.use(
      http.get('*/api/events', async () => {
        calls += 1;
        if (calls === 1) {
          await new Promise<void>((r) => (releaseFirst = r));
          return HttpResponse.json([stale]);
        }
        return HttpResponse.json([fresh]);
      }),
    );
    renderApp('/overview');
    await waitFor(() => expect(calls).toBe(1));
    act(() => {
      vi.advanceTimersByTime(10_100);
    });
    const feed = await screen.findByRole('list', { name: /recent events/i });
    await waitFor(() => expect(feed).toHaveTextContent(/FRESH actor/));
    releaseFirst();
    await new Promise((r) => setTimeout(r, 50));
    expect(feed).toHaveTextContent(/FRESH actor/);
    expect(feed).not.toHaveTextContent(/STALE actor/);
    expect(screen.queryByRole('alert')).toBeNull(); // an aborted request is not an error
  });
});
