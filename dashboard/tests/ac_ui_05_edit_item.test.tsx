// AC-UI-5 Edit item form: label, description, catalog, location, status, operator/note. PATCH with only changed
// fields; success/error shown.
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import { drill, drillDetail, ID } from './fixtures';
import { requestsTo, server } from './server';
import { findToast, renderApp } from './render';
import { changedFields, type EditFormValues } from '../src/lib/diff';

const patchPath = `/api/items/${ID.drill}`;

async function openEdit() {
  const r = renderApp(`/items/${ID.drill}`);
  await screen.findByRole('heading', { name: /pistol-grip drill/i });
  await r.user.click(screen.getByRole('button', { name: /^edit/i }));
  const form = await screen.findByRole('form', { name: /edit item/i });
  // wait for catalog + locations to be loaded into the selects
  await waitFor(() => expect((within(form).getByLabelText(/catalog/i) as HTMLSelectElement).options.length).toBeGreaterThan(1));
  await waitFor(() => expect((within(form).getByLabelText(/location/i) as HTMLSelectElement).options.length).toBeGreaterThan(1));
  return { ...r, form };
}

describe('AC-UI-5 edit item', () => {
  test('AC-UI-5 form has label, description, catalog, location, status, operator and note fields', async () => {
    const { form } = await openEdit();
    for (const re of [/^label$/i, /description/i, /catalog/i, /location/i, /status/i, /operator/i, /note/i]) {
      expect(within(form).getByLabelText(re)).toBeInTheDocument();
    }
  });

  test('AC-UI-5 form is prefilled with the current values', async () => {
    const { form } = await openEdit();
    expect(within(form).getByLabelText(/^label$/i)).toHaveValue(drill.label);
    expect(within(form).getByLabelText(/description/i)).toHaveValue(drill.description);
    expect(within(form).getByLabelText(/catalog/i)).toHaveValue('drill-pistol-grip');
    expect(within(form).getByLabelText(/location/i)).toHaveValue('LAB1S2');
    expect(within(form).getByLabelText(/status/i)).toHaveValue('confirmed');
  });

  test('AC-UI-5 status options exclude "merged" (not a PATCHable status)', async () => {
    const { form } = await openEdit();
    const values = Array.from((within(form).getByLabelText(/status/i) as HTMLSelectElement).options).map((o) => o.value);
    expect(values.sort()).toEqual(['confirmed', 'dismissed', 'provisional', 'unknown']);
  });

  test('AC-UI-5 PATCH contains only the changed field (+ operator/note)', async () => {
    const { user, form } = await openEdit();
    const label = within(form).getByLabelText(/^label$/i);
    await user.clear(label);
    await user.type(label, 'Cordless drill');
    await user.type(within(form).getByLabelText(/operator/i), 'alice');
    await user.type(within(form).getByLabelText(/note/i), 'relabelled after inspection');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    await waitFor(() => expect(requestsTo('PATCH', patchPath)).toHaveLength(1));
    expect(requestsTo('PATCH', patchPath)[0].body).toEqual({
      label: 'Cordless drill', operator: 'alice', note: 'relabelled after inspection',
    });
  });

  test('AC-UI-5 location change is sent as last_location; status change as status', async () => {
    const { user, form } = await openEdit();
    await user.selectOptions(within(form).getByLabelText(/location/i), 'NOD2D4');
    await user.selectOptions(within(form).getByLabelText(/status/i), 'provisional');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    await waitFor(() => expect(requestsTo('PATCH', patchPath)).toHaveLength(1));
    expect(requestsTo('PATCH', patchPath)[0].body).toEqual({ last_location: 'NOD2D4', status: 'provisional' });
  });

  test('AC-UI-5 clearing the catalog sends catalog_id: null', async () => {
    const { user, form } = await openEdit();
    await user.selectOptions(within(form).getByLabelText(/catalog/i), '');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    await waitFor(() => expect(requestsTo('PATCH', patchPath)).toHaveLength(1));
    expect(requestsTo('PATCH', patchPath)[0].body).toEqual({ catalog_id: null });
  });

  test('AC-UI-5 description change only', async () => {
    const { user, form } = await openEdit();
    const d = within(form).getByLabelText(/description/i);
    await user.clear(d);
    await user.type(d, 'Has a scratched grip');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    await waitFor(() => expect(requestsTo('PATCH', patchPath)).toHaveLength(1));
    expect(requestsTo('PATCH', patchPath)[0].body).toEqual({ description: 'Has a scratched grip' });
  });

  test('AC-UI-5 no changes -> no PATCH is sent', async () => {
    const { user, form } = await openEdit();
    const save = within(form).getByRole('button', { name: /save/i });
    if (!(save as HTMLButtonElement).disabled) await user.click(save);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('PATCH', patchPath)).toHaveLength(0);
  });

  test('AC-UI-5 operator/note alone (no field change) does not PATCH', async () => {
    const { user, form } = await openEdit();
    await user.type(within(form).getByLabelText(/operator/i), 'alice');
    const save = within(form).getByRole('button', { name: /save/i });
    if (!(save as HTMLButtonElement).disabled) await user.click(save);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('PATCH', patchPath)).toHaveLength(0);
  });

  test('AC-UI-5 success is shown and the detail reflects the new value', async () => {
    const { user, form } = await openEdit();
    const label = within(form).getByLabelText(/^label$/i);
    await user.clear(label);
    await user.type(label, 'Cordless drill');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    expect(await findToast(/saved|updated/i)).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: /cordless drill/i })).toBeInTheDocument();
  });

  test('AC-UI-5 API error (409 merged) is shown and the form keeps the user input', async () => {
    server.use(
      http.patch(`*/api/items/${ID.drill}`, () => HttpResponse.json({ detail: 'entity is merged' }, { status: 409 })),
    );
    const { user, form } = await openEdit();
    const label = within(form).getByLabelText(/^label$/i);
    await user.clear(label);
    await user.type(label, 'Cordless drill');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    expect(await findToast(/entity is merged|409|failed|error/i)).toBeInTheDocument();
    expect(screen.getByRole('form', { name: /edit item/i })).toBeInTheDocument();
    expect(within(screen.getByRole('form', { name: /edit item/i })).getByLabelText(/^label$/i)).toHaveValue('Cordless drill');
  });
});

describe('AC-UI-5 changedFields helper', () => {
  const base: EditFormValues = {
    label: drill.label, description: drill.description ?? '', catalog_id: drill.catalog_id ?? '',
    last_location: drill.last_location ?? '', status: drill.status, operator: '', note: '',
  };

  test('AC-UI-5 unchanged values -> empty patch', () => {
    expect(changedFields(drill, base)).toEqual({});
  });

  test('AC-UI-5 only changed keys appear; catalog cleared -> null', () => {
    expect(changedFields(drill, { ...base, label: 'X', catalog_id: '' })).toEqual({ label: 'X', catalog_id: null });
  });

  test('AC-UI-5 operator and note are included only alongside a real change', () => {
    expect(changedFields(drill, { ...base, operator: 'bob', note: 'n' })).toEqual({});
    expect(changedFields(drill, { ...base, status: 'dismissed', operator: 'bob', note: 'n' })).toEqual({
      status: 'dismissed', operator: 'bob', note: 'n',
    });
  });

  test('AC-UI-5 whitespace-only label edit is trimmed and treated as unchanged', () => {
    expect(changedFields(drill, { ...base, label: `  ${drill.label}  ` })).toEqual({});
  });

  test('AC-UI-5 null original description vs empty input is unchanged', () => {
    expect(changedFields({ ...drill, description: null }, { ...base, description: '' })).toEqual({});
  });
});

describe('AC-UI-5 review round 1', () => {
  test('AC-UI-5 clearing the label shows "Label is required" and Save stays disabled', async () => {
    const { user, form } = await openEdit();
    await user.clear(within(form).getByLabelText(/^label$/i));
    expect(within(form).getByText(/label is required/i)).toBeInTheDocument();
    expect(within(form).getByLabelText(/^label$/i)).toHaveAttribute('aria-invalid', 'true');
    expect(within(form).getByRole('button', { name: /save/i })).toBeDisabled();
  });

  test('AC-UI-5 clearing the label while changing another field still blocks Save (label never silently dropped)', async () => {
    const { user, form } = await openEdit();
    await user.clear(within(form).getByLabelText(/^label$/i));
    await user.selectOptions(within(form).getByLabelText(/status/i), 'provisional');
    const save = within(form).getByRole('button', { name: /save/i });
    expect(save).toBeDisabled();
    await new Promise((r) => setTimeout(r, 20));
    expect(requestsTo('PATCH', patchPath)).toHaveLength(0);
  });

  test('AC-UI-5 the operator is prefilled from the shared operator (localStorage) and sent with the PATCH', async () => {
    window.localStorage.setItem('nasa-omni.operator', 'carol');
    const { user, form } = await openEdit();
    expect(within(form).getByLabelText(/operator/i)).toHaveValue('carol');
    await user.selectOptions(within(form).getByLabelText(/status/i), 'provisional');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    await waitFor(() => expect(requestsTo('PATCH', patchPath)).toHaveLength(1));
    expect(requestsTo('PATCH', patchPath)[0].body).toEqual({ status: 'provisional', operator: 'carol' });
  });

  test('AC-UI-5 after a save the item is reloaded so Movements and last moved by are current', async () => {
    let saved = false;
    const newMove = {
      event_id: 'e0000000-0000-4000-8000-0000000000f1', event_type: 'moved', entity_id: ID.drill,
      actor_desc: 'operator dave', from_location: 'LAB1S2', to_location: 'NOD2D4', occurred_at: '2026-10-17T12:30:00Z',
      confidence: 1, source: 'operator',
    };
    server.use(
      http.get(`*/api/items/${ID.drill}`, () =>
        HttpResponse.json(saved
          ? { ...drillDetail, last_location: 'NOD2D4', last_moved_by: 'operator dave', events: [newMove, ...(drillDetail.events ?? [])] }
          : drillDetail),
      ),
      http.patch(`*/api/items/${ID.drill}`, async ({ request }) => {
        saved = true;
        const { operator: _o, note: _n, ...body } = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ ...drill, ...body });
      }),
    );
    const { user, form } = await openEdit();
    await user.selectOptions(within(form).getByLabelText(/location/i), 'NOD2D4');
    await user.click(within(form).getByRole('button', { name: /save/i }));
    const moves = await screen.findByRole('list', { name: /movements/i });
    await waitFor(() => expect(moves).toHaveTextContent(/operator dave/));
    expect(screen.getAllByText(/operator dave/).length).toBeGreaterThanOrEqual(2); // movement + last moved by
    expect(requestsTo('GET', `/api/items/${ID.drill}`).length).toBeGreaterThanOrEqual(2);
  });
});
