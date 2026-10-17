// AC-UI-2 Filters: debounced search, kind, status (multi), hierarchical location, min confidence, time range,
// needs-review toggle; reflected in the URL and mapped 1:1 to GET /items params.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { NOW } from './fixtures';
import { lastItemsQuery, requestsTo } from './server';
import { fakeTimers, findItem, renderApp, searchParams } from './render';
import { ID } from './fixtures';
import { filtersFromSearch, filtersToItemsQuery, filtersToSearch } from '../src/lib/filters';
import { flattenLocationTree } from '../src/lib/locations';
import { LOCATIONS } from './fixtures';

const ALLOWED_ITEMS_PARAMS = new Set([
  'q', 'kind', 'status', 'catalog_id', 'location', 'min_confidence', 'seen_after', 'seen_before', 'needs_review',
  'sort', 'limit', 'offset',
]);

async function ready(path = '/') {
  const r = renderApp(path);
  await findItem(ID.drill);
  return r;
}

describe('AC-UI-2 filters', () => {
  test('AC-UI-2 search is debounced 300 ms and sends a single q= request', async () => {
    fakeTimers(NOW);
    const { user } = renderApp('/', { fakeTimers: true });
    await findItem(ID.drill);
    await user.type(screen.getByLabelText(/search/i), 'drill');
    act(() => {
      vi.advanceTimersByTime(250);
    });
    expect(requestsTo('GET', '/api/items').some((r) => r.url.searchParams.has('q'))).toBe(false);
    act(() => {
      vi.advanceTimersByTime(100);
    });
    await waitFor(() => expect(lastItemsQuery()?.get('q')).toBe('drill'));
    const withQ = requestsTo('GET', '/api/items').filter((r) => r.url.searchParams.has('q'));
    expect(withQ).toHaveLength(1);
    expect(searchParams().get('q')).toBe('drill');
  });

  test('AC-UI-2 kind select maps to kind=', async () => {
    const { user } = await ready();
    await user.selectOptions(screen.getByLabelText(/^kind$/i), 'person');
    await waitFor(() => expect(lastItemsQuery()?.get('kind')).toBe('person'));
    expect(searchParams().get('kind')).toBe('person');
  });

  test('AC-UI-2 status is multi-select and sent as repeated status= params (form/explode)', async () => {
    const { user } = await ready();
    const group = screen.getByRole('group', { name: /status/i });
    for (const s of ['confirmed', 'provisional', 'unknown', 'merged', 'dismissed']) {
      expect(within(group).getByRole('checkbox', { name: new RegExp(`^${s}$`, 'i') })).toBeInTheDocument();
    }
    await user.click(within(group).getByRole('checkbox', { name: /^confirmed$/i }));
    await user.click(within(group).getByRole('checkbox', { name: /^unknown$/i }));
    await waitFor(() => expect(lastItemsQuery()?.getAll('status').sort()).toEqual(['confirmed', 'unknown']));
    expect(searchParams().getAll('status').sort()).toEqual(['confirmed', 'unknown']);
    // un-checking removes it
    await user.click(within(group).getByRole('checkbox', { name: /^confirmed$/i }));
    await waitFor(() => expect(lastItemsQuery()?.getAll('status')).toEqual(['unknown']));
  });

  test('AC-UI-2 location select is hierarchical (parents before their children) and maps to location=', async () => {
    const { user } = await ready();
    const select = screen.getByLabelText(/^location$/i) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThanOrEqual(LOCATIONS.length));
    const values = Array.from(select.options).map((o) => o.value);
    const idx = (v: string) => values.indexOf(v);
    expect(idx('USOS')).toBeLessThan(idx('NOD2'));
    expect(idx('NOD2')).toBeLessThan(idx('NOD2D4'));
    expect(idx('NOD2')).toBeLessThan(idx('NOD2O4'));
    expect(idx('LAB1')).toBeLessThan(idx('LAB1S2'));
    // children are grouped directly under their parent: NOD2's zones are not after LAB1's
    const nod2Kids = [idx('NOD2D4'), idx('NOD2O4')];
    const lab1Kids = [idx('LAB1S2'), idx('LAB1P3')];
    expect(Math.max(...nod2Kids) < idx('LAB1') || Math.max(...lab1Kids) < idx('NOD2')).toBe(true);
    // display names are shown
    expect(Array.from(select.options).some((o) => /Node 2 deck bay 4/.test(o.textContent ?? ''))).toBe(true);
    await user.selectOptions(select, 'NOD2');
    await waitFor(() => expect(lastItemsQuery()?.get('location')).toBe('NOD2'));
    expect(searchParams().get('location')).toBe('NOD2');
  });

  test('AC-UI-2 min confidence slider maps to min_confidence= (0..1)', async () => {
    await ready();
    const slider = screen.getByLabelText(/min(imum)?\.? confidence/i) as HTMLInputElement;
    expect(slider.type).toBe('range');
    expect(Number(slider.min)).toBe(0);
    expect(Number(slider.max)).toBe(1);
    fireEvent.change(slider, { target: { value: '0.5' } });
    await waitFor(() => expect(Number(lastItemsQuery()?.get('min_confidence'))).toBe(0.5));
    expect(Number(searchParams().get('min_confidence'))).toBe(0.5);
  });

  test('AC-UI-2 time range maps to seen_after / seen_before (ISO-8601 UTC)', async () => {
    await ready();
    fireEvent.change(screen.getByLabelText(/seen after/i), { target: { value: '2026-10-17T10:00' } });
    fireEvent.change(screen.getByLabelText(/seen before/i), { target: { value: '2026-10-17T11:30' } });
    await waitFor(() => {
      const q = lastItemsQuery()!;
      expect(Date.parse(q.get('seen_after') ?? '')).toBe(Date.parse('2026-10-17T10:00:00Z'));
      expect(Date.parse(q.get('seen_before') ?? '')).toBe(Date.parse('2026-10-17T11:30:00Z'));
    });
    expect(Date.parse(searchParams().get('seen_after') ?? '')).toBe(Date.parse('2026-10-17T10:00:00Z'));
  });

  test('AC-UI-2 needs-review toggle sends needs_review=true, and nothing when off', async () => {
    const { user } = await ready();
    const toggle = screen.getByRole('checkbox', { name: /needs review/i });
    await user.click(toggle);
    await waitFor(() => expect(lastItemsQuery()?.get('needs_review')).toBe('true'));
    expect(searchParams().get('needs_review')).toBe('true');
    await user.click(toggle);
    await waitFor(() => expect(lastItemsQuery()?.has('needs_review')).toBe(false));
    expect(searchParams().has('needs_review')).toBe(false);
  });

  test('AC-UI-2 filters are restored from a shared URL: controls prefilled and first request carries them', async () => {
    const qs =
      '?q=drill&kind=object&status=confirmed&status=unknown&location=LAB1&min_confidence=0.6&needs_review=true' +
      '&seen_after=2026-10-17T10%3A00%3A00Z';
    renderApp('/' + qs);
    await waitFor(() => expect(requestsTo('GET', '/api/items').length).toBeGreaterThan(0));
    const first = requestsTo('GET', '/api/items')[0].url.searchParams;
    expect(first.get('q')).toBe('drill');
    expect(first.get('kind')).toBe('object');
    expect(first.getAll('status').sort()).toEqual(['confirmed', 'unknown']);
    expect(first.get('location')).toBe('LAB1');
    expect(Number(first.get('min_confidence'))).toBe(0.6);
    expect(first.get('needs_review')).toBe('true');
    expect(Date.parse(first.get('seen_after') ?? '')).toBe(Date.parse('2026-10-17T10:00:00Z'));

    expect(screen.getByLabelText(/search/i)).toHaveValue('drill');
    expect(screen.getByLabelText(/^kind$/i)).toHaveValue('object');
    const group = screen.getByRole('group', { name: /status/i });
    expect(within(group).getByRole('checkbox', { name: /^confirmed$/i })).toBeChecked();
    expect(within(group).getByRole('checkbox', { name: /^unknown$/i })).toBeChecked();
    expect(within(group).getByRole('checkbox', { name: /^provisional$/i })).not.toBeChecked();
    await waitFor(() => expect(screen.getByLabelText(/^location$/i)).toHaveValue('LAB1'));
    expect(Number((screen.getByLabelText(/min(imum)?\.? confidence/i) as HTMLInputElement).value)).toBe(0.6);
    expect(screen.getByRole('checkbox', { name: /needs review/i })).toBeChecked();
  });

  test('AC-UI-2 requests only use /items parameter names and never send empty values', async () => {
    const { user } = await ready();
    await user.selectOptions(screen.getByLabelText(/^kind$/i), 'object');
    await waitFor(() => expect(lastItemsQuery()?.get('kind')).toBe('object'));
    for (const r of requestsTo('GET', '/api/items')) {
      for (const [k, v] of r.url.searchParams.entries()) {
        expect(ALLOWED_ITEMS_PARAMS.has(k)).toBe(true);
        expect(v).not.toBe('');
      }
    }
  });

  test('AC-UI-2 clearing the kind filter removes kind= from URL and request', async () => {
    const { user } = renderApp('/?kind=person');
    await waitFor(() => expect(lastItemsQuery()?.get('kind')).toBe('person'));
    await user.selectOptions(screen.getByLabelText(/^kind$/i), '');
    await waitFor(() => expect(lastItemsQuery()?.has('kind')).toBe(false));
    expect(searchParams().has('kind')).toBe(false);
  });

  test('AC-UI-2 changing a filter resets pagination to offset=0', async () => {
    const { user } = renderApp('/?offset=50');
    await waitFor(() => expect(lastItemsQuery()?.get('offset')).toBe('50'));
    await user.selectOptions(screen.getByLabelText(/^kind$/i), 'object');
    await waitFor(() => expect(lastItemsQuery()?.get('kind')).toBe('object'));
    expect(lastItemsQuery()?.get('offset') ?? '0').toBe('0');
  });

  test('AC-UI-2 invalid URL values are ignored, not forwarded (status=bogus, min_confidence=7)', async () => {
    renderApp('/?status=bogus&status=confirmed&min_confidence=7&kind=robot');
    await waitFor(() => expect(requestsTo('GET', '/api/items').length).toBeGreaterThan(0));
    const q = requestsTo('GET', '/api/items')[0].url.searchParams;
    expect(q.getAll('status')).toEqual(['confirmed']);
    expect(q.has('min_confidence')).toBe(false);
    expect(q.has('kind')).toBe(false);
  });
});

describe('AC-UI-2 filter mapping helpers (lib/filters)', () => {
  test('AC-UI-2 filtersFromSearch parses every filter key', () => {
    const f = filtersFromSearch(
      'q=drill&kind=person&status=confirmed&status=unknown&location=NOD2&min_confidence=0.25' +
        '&seen_after=2026-10-17T10:00:00Z&seen_before=2026-10-17T11:00:00Z&needs_review=true&sort=label&offset=100',
    );
    expect(f).toMatchObject({
      q: 'drill', kind: 'person', status: ['confirmed', 'unknown'], location: 'NOD2', min_confidence: 0.25,
      needs_review: true, sort: 'label', offset: 100,
    });
    expect(Date.parse(f.seen_after!)).toBe(Date.parse('2026-10-17T10:00:00Z'));
    expect(Date.parse(f.seen_before!)).toBe(Date.parse('2026-10-17T11:00:00Z'));
  });

  test('AC-UI-2 filtersFromSearch drops invalid enum / range values', () => {
    const f = filtersFromSearch('kind=robot&status=nope&min_confidence=1.5&sort=random&offset=-5&needs_review=maybe');
    expect(f.kind).toBeUndefined();
    expect(f.status ?? []).toEqual([]);
    expect(f.min_confidence).toBeUndefined();
    expect(f.sort === undefined || f.sort === '-last_seen_at').toBe(true);
    expect(f.offset ?? 0).toBe(0);
    expect(f.needs_review).toBeUndefined();
  });

  test('AC-UI-2 filtersToSearch omits empty values and repeats status', () => {
    const s = filtersToSearch({ q: '', kind: undefined, status: ['provisional', 'merged'], needs_review: false });
    expect(s.has('q')).toBe(false);
    expect(s.has('kind')).toBe(false);
    expect(s.getAll('status')).toEqual(['provisional', 'merged']);
    expect(s.has('needs_review')).toBe(false);
  });

  test('AC-UI-2/3 filtersToItemsQuery maps 1:1 and always includes sort, limit=50 and offset', () => {
    const q = filtersToItemsQuery({ q: 'cam', location: 'LAB1', min_confidence: 0.3, needs_review: true });
    expect(q.get('q')).toBe('cam');
    expect(q.get('location')).toBe('LAB1');
    expect(q.get('min_confidence')).toBe('0.3');
    expect(q.get('needs_review')).toBe('true');
    expect(q.get('sort')).toBe('-last_seen_at');
    expect(q.get('limit')).toBe('50');
    expect(q.get('offset')).toBe('0');
    for (const k of q.keys()) expect(ALLOWED_ITEMS_PARAMS.has(k)).toBe(true);
  });

  test('AC-UI-2 round trip URL -> filters -> URL is stable', () => {
    const qs = 'q=drill&kind=object&status=confirmed&status=unknown&location=LAB1&min_confidence=0.6&needs_review=true';
    const back = filtersToSearch(filtersFromSearch(qs));
    const orig = new URLSearchParams(qs);
    for (const k of new Set(orig.keys())) expect(back.getAll(k).sort()).toEqual(orig.getAll(k).sort());
  });

  test('AC-UI-2 flattenLocationTree orders parents before children with depth', () => {
    const flat = flattenLocationTree(LOCATIONS);
    expect(flat).toHaveLength(LOCATIONS.length);
    const pos = Object.fromEntries(flat.map((o, i) => [o.location_id, i]));
    expect(pos.USOS).toBe(0);
    expect(pos.NOD2).toBeLessThan(pos.NOD2D4);
    expect(pos.LAB1).toBeLessThan(pos.LAB1P3);
    const depth = Object.fromEntries(flat.map((o) => [o.location_id, o.depth]));
    expect(depth).toMatchObject({ USOS: 0, NOD2: 1, LAB1: 1, NOD2D4: 2, LAB1S2: 2 });
  });

  test('AC-UI-2 flattenLocationTree tolerates orphans (unknown parent) without dropping them', () => {
    const flat = flattenLocationTree([...LOCATIONS, { location_id: 'X1', parent_id: 'MISSING', display_name: 'Orphan' }]);
    expect(flat.map((o) => o.location_id)).toContain('X1');
  });
});

describe('AC-UI-2 review round 1', () => {
  test('AC-UI-2 a failed fetch after a filter change does not leave the previous rows looking current', async () => {
    const { user } = await ready('/');
    const { server } = await import('./server');
    const { http, HttpResponse } = await import('msw');
    server.use(http.get('*/api/items', () => HttpResponse.json({ detail: 'database unavailable' }, { status: 500 })));
    await user.selectOptions(screen.getByLabelText(/^kind$/i), 'person');
    await waitFor(() => expect(lastItemsQuery()?.get('kind')).toBe('person'));
    await waitFor(() => expect(screen.queryByTestId(`item-${ID.drill}`)).toBeNull());
    expect(screen.getByText(/inventory unavailable/i)).toBeInTheDocument();
    expect(screen.queryByText(/^\d+ items?$/)).toBeNull();
  });

  test('AC-UI-2 offset-less seen_after in a shared URL is read as UTC regardless of the browser time zone', () => {
    const prev = process.env.TZ;
    process.env.TZ = 'America/New_York';
    try {
      expect(new Date(2026, 9, 17, 10, 0).getTimezoneOffset()).not.toBe(0); // the TZ switch really took effect
      expect(filtersFromSearch('seen_after=2026-10-17T10:00').seen_after).toBe('2026-10-17T10:00:00.000Z');
      expect(filtersFromSearch('seen_before=2026-10-17T10:00:30').seen_before).toBe('2026-10-17T10:00:30.000Z');
      // explicit offsets are honoured
      expect(filtersFromSearch('seen_after=2026-10-17T10:00:00%2B02:00').seen_after).toBe('2026-10-17T08:00:00.000Z');
      expect(filtersFromSearch('seen_after=2026-10-17T10:00:00Z').seen_after).toBe('2026-10-17T10:00:00.000Z');
    } finally {
      process.env.TZ = prev;
    }
  });
});
