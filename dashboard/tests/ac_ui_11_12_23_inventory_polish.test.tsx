// UI review fixes on the inventory, item facts and forms:
//   AC-UI-11 visible absolute UTC + relative time; AC-UI-12 "Last handled" (natural actor phrasing, fallback);
//   AC-UI-23 UTC text date inputs + quick ranges, reset filters, slider valuetext, skip link, malformed id, add-form errors.
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import { ID, NOW, drill, drillDetail } from './fixtures';
import { lastItemsQuery, requestsTo, server } from './server';
import { findItem, freezeDate, renderApp, searchParams } from './render';
import { actorPhrase, formatUtcMinute, handledPhrase, locationLabel } from '../src/lib/format';

const handled = {
  ...drill,
  last_handled: { event_type: 'used', actor_desc: 'astronaut in blue suit', occurred_at: '2026-10-17T11:42:00Z' },
};

describe('AC-UI-11 timestamps are absolute UTC with the relative time beside them', () => {
  test('AC-UI-11 formatUtcMinute renders "YYYY-MM-DD HH:MM UTC" and nothing for bad input', () => {
    expect(formatUtcMinute('2026-10-17T21:42:59Z')).toBe('2026-10-17 21:42 UTC');
    expect(formatUtcMinute(null)).toBe('');
    expect(formatUtcMinute('not a date')).toBe('');
  });

  test('AC-UI-11 inventory Last seen shows the UTC time visibly (not only in a title) next to "5 min ago"', async () => {
    freezeDate(NOW);
    renderApp('/');
    const row = await findItem(ID.drill);
    const time = within(row).getByText('2026-10-17 12:00 UTC');
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('datetime', drill.last_seen_at);
    expect(within(row).getByText(/5 min ago/)).toBeVisible();
  });

  test('AC-UI-11 item detail shows Last sighting as absolute UTC + relative', async () => {
    freezeDate(NOW);
    renderApp(`/items/${ID.drill}`);
    await screen.findByRole('heading', { name: /pistol-grip drill/i });
    expect(screen.getByText('Last sighting')).toBeInTheDocument();
    expect(screen.getAllByText('2026-10-17 12:00 UTC').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/5 min ago/).length).toBeGreaterThan(0);
  });
});

describe('AC-UI-12 Last handled', () => {
  test('AC-UI-12 actorPhrase reads naturally after "by"', () => {
    expect(actorPhrase('astronaut in blue suit')).toBe('astronaut in blue suit');
    expect(actorPhrase('wearing a blue tank top and a headset')).toBe('person wearing a blue tank top and a headset');
    expect(actorPhrase('Blue shirt, black shorts, headset')).toBe('person (blue shirt, black shorts, headset)');
    expect(actorPhrase('sleeping, wearing a headset')).toBe('person (sleeping, wearing a headset)');
    expect(actorPhrase('crew member, blue shirt')).toBe('crew member, blue shirt');
    expect(actorPhrase('  ')).toBe('an unknown person');
    expect(actorPhrase(null)).toBe('an unknown person');
    expect(handledPhrase('handed_over', 'astronaut')).toBe('Handed over by astronaut');
    expect(handledPhrase('used', 'wearing a blue tank top')).toBe('Used by person wearing a blue tank top');
  });

  test('AC-UI-12 locationLabel gives "Name (CODE)", the code alone, or nothing', () => {
    expect(locationLabel('NOD1', 'Node 1 Unity')).toBe('Node 1 Unity (NOD1)');
    expect(locationLabel('NOD1', null)).toBe('NOD1');
    expect(locationLabel(null, null)).toBe('');
  });

  test('AC-UI-12 the column is called "Last handled" and renders verb + actor + UTC time from last_handled', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.json({ items: [handled], total: 1 })));
    renderApp('/');
    expect(await screen.findByRole('columnheader', { name: /last handled/i })).toBeInTheDocument();
    expect(screen.queryByRole('columnheader', { name: /moved by/i })).toBeNull();
    const row = await findItem(ID.drill);
    expect(within(row).getByText('Used by astronaut in blue suit')).toBeInTheDocument();
    expect(within(row).getByText('2026-10-17 11:42 UTC')).toBeInTheDocument();
  });

  test('AC-UI-12 raw fragments never read "Moved by wearing ..."', async () => {
    server.use(
      http.get('*/api/items', () =>
        HttpResponse.json({
          items: [{ ...drill, last_handled: { event_type: 'moved', actor_desc: 'wearing a blue tank top', occurred_at: '2026-10-17T11:00:00Z' } }],
          total: 1,
        }),
      ),
    );
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(within(row).getByText('Moved by person wearing a blue tank top')).toBeInTheDocument();
    expect(row.textContent).not.toMatch(/Moved by wearing/);
  });

  test('AC-UI-12 without last_handled it falls back to last_moved_by / last_moved_at', async () => {
    renderApp('/');
    const row = await findItem(ID.drill);
    expect(within(row).getByText('Moved by crew member, blue shirt')).toBeInTheDocument();
    expect(within(row).getByText('2026-10-17 11:00 UTC')).toBeInTheDocument();
    const none = await findItem(ID.camera);
    expect(none.textContent).not.toMatch(/by /);
  });

  test('AC-UI-12 detail shows "Location set by" (operator edit) separately from the last sighting', async () => {
    server.use(
      http.get(`*/api/items/${ID.drill}`, () =>
        HttpResponse.json({ ...drillDetail, location_set_by: 'ui-review', location_set_at: '2026-10-18T00:06:31Z' }),
      ),
    );
    renderApp(`/items/${ID.drill}`);
    await screen.findByRole('heading', { name: /pistol-grip drill/i });
    expect(screen.getByText('Location set')).toBeInTheDocument();
    expect(screen.getByText(/by ui-review at/)).toBeInTheDocument();
    expect(screen.getByText('2026-10-18 00:06 UTC')).toBeInTheDocument();
  });

  test('AC-UI-12 detail without location_set_* shows no "Location set" fact', async () => {
    renderApp(`/items/${ID.drill}`);
    await screen.findByRole('heading', { name: /pistol-grip drill/i });
    expect(screen.queryByText('Location set')).toBeNull();
  });
});

describe('AC-UI-23 inventory filter polish', () => {
  async function ready() {
    const r = renderApp('/');
    await findItem(ID.drill);
    return r;
  }

  test('AC-UI-23 seen after/before are 24 h UTC text inputs (no locale picker) that accept "YYYY-MM-DD HH:MM"', async () => {
    const { user } = await ready();
    const after = screen.getByLabelText(/seen after/i) as HTMLInputElement;
    expect(after.type).toBe('text');
    expect(after.placeholder).toBe('YYYY-MM-DD HH:MM');
    await user.type(after, '2026-10-17 10:00');
    await waitFor(() => expect(Date.parse(lastItemsQuery()?.get('seen_after') ?? '')).toBe(Date.parse('2026-10-17T10:00:00Z')));
  });

  test('AC-UI-23 an incomplete date is flagged and not sent', async () => {
    await ready();
    const after = screen.getByLabelText(/seen after/i);
    fireEvent.change(after, { target: { value: '2026-10-17 10' } });
    expect(after).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByText(/use YYYY-MM-DD HH:MM/i)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(lastItemsQuery()?.has('seen_after')).toBe(false);
  });

  test('AC-UI-23 quick range "Last 1 h" sets seen_after to now - 1 h', async () => {
    const { user } = await ready();
    const before = Date.now();
    await user.click(screen.getByRole('button', { name: /last 1 h/i }));
    await waitFor(() => expect(lastItemsQuery()?.has('seen_after')).toBe(true));
    const t = Date.parse(lastItemsQuery()!.get('seen_after')!);
    expect(Math.abs(before - 3600_000 - t)).toBeLessThan(10_000);
  });

  test('AC-UI-23 Reset filters clears every filter (keeps sort) and is disabled when nothing is set', async () => {
    renderApp('/?kind=object&location=LAB1&q=drill&sort=label');
    await findItem(ID.drill);
    const reset = screen.getByRole('button', { name: /reset filters/i });
    expect(reset).toBeEnabled();
    fireEvent.click(reset);
    await waitFor(() => expect(searchParams().has('kind')).toBe(false));
    expect(searchParams().has('location')).toBe(false);
    expect(searchParams().has('q')).toBe(false);
    expect(searchParams().get('sort')).toBe('label');
    expect(screen.getByLabelText(/^search$/i)).toHaveValue('');
    await waitFor(() => expect(screen.getByRole('button', { name: /reset filters/i })).toBeDisabled());
  });

  test('AC-UI-23 the confidence slider announces its value (aria-valuetext)', async () => {
    renderApp('/?min_confidence=0.6');
    await findItem(ID.drill);
    expect(screen.getByLabelText(/min confidence/i)).toHaveAttribute('aria-valuetext', '60%');
  });

  test('AC-UI-23 a "Skip to content" link targets the main region', async () => {
    await ready();
    const skip = screen.getByRole('link', { name: /skip to content/i });
    expect(skip).toHaveAttribute('href', '#main');
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main');
  });
});

describe('AC-UI-23 item not found for malformed ids', () => {
  test('AC-UI-23 /items/not-a-real-id shows "Item not found." without calling the API', async () => {
    renderApp('/items/not-a-real-id');
    expect(await screen.findByText(/item not found/i)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /back to inventory/i })).toBeInTheDocument();
    expect(requestsTo('GET', /\/api\/items\/not-a-real-id/)).toHaveLength(0);
  });

  test('AC-UI-23 a 422 from the API is treated as not found (no error toast)', async () => {
    const id = '77777777-7777-4777-8777-777777777777';
    server.use(http.get(`*/api/items/${id}`, () => HttpResponse.json({ detail: 'invalid uuid' }, { status: 422 })));
    renderApp(`/items/${id}`);
    expect(await screen.findByText(/item not found/i)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

describe('AC-UI-23 add form explains every missing required field', () => {
  test('AC-UI-23 label and location errors appear once touched, with aria-invalid and aria-describedby', async () => {
    const { user } = renderApp('/items/new');
    const label = await screen.findByLabelText(/^label$/i);
    const location = screen.getByLabelText(/^location$/i);
    expect(screen.queryByText(/label is required/i)).toBeNull();
    await user.click(label);
    await user.click(location);
    await user.click(screen.getByLabelText(/description/i));
    expect(screen.getByText(/label is required/i)).toBeInTheDocument();
    expect(screen.getByText(/location is required/i)).toBeInTheDocument();
    expect(label).toHaveAttribute('aria-invalid', 'true');
    expect(label.getAttribute('aria-describedby')).toBeTruthy();
    expect(document.getElementById(label.getAttribute('aria-describedby')!)).toHaveTextContent(/label is required/i);
    expect(location).toHaveAttribute('aria-invalid', 'true');
  });

  test('AC-UI-23 the disabled submit is described by a list of what is missing', async () => {
    renderApp('/items/new');
    const btn = await screen.findByRole('button', { name: /add item/i });
    expect(btn).toBeDisabled();
    expect(btn).toHaveAccessibleDescription(/provide an image, a label, a location/i);
  });
});
