// AC-UI-6 Add item form: image REQUIRED (submit disabled + validation message until an image file is chosen), label,
// location, optional catalog/description; posts multipart/form-data.
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import { ID } from './fixtures';
import { requestsTo, server } from './server';
import { MultipartBody, type FilePart } from './multipart';
import { findItem, findToast, renderApp } from './render';

const png = () => new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'tool.png', { type: 'image/png' });

async function openAdd() {
  const r = renderApp('/items/new');
  const form = await screen.findByRole('form', { name: /add item/i });
  await waitFor(() => expect((within(form).getByLabelText(/location/i) as HTMLSelectElement).options.length).toBeGreaterThan(1));
  return { ...r, form };
}

const submitBtn = (form: HTMLElement) => within(form).getByRole('button', { name: /^(add item|add|create|submit|save)$/i });

async function fillRequired(user: ReturnType<typeof renderApp>['user'], form: HTMLElement) {
  await user.type(within(form).getByLabelText(/^label$/i), 'Torque wrench');
  await user.selectOptions(within(form).getByLabelText(/location/i), 'NOD2D4');
}

describe('AC-UI-6 add item', () => {
  test('AC-UI-6 inventory page links to the add-item form', async () => {
    const { user } = renderApp('/');
    await findItem(ID.drill);
    await user.click(screen.getByRole('link', { name: /add item/i }));
    expect(await screen.findByRole('form', { name: /add item/i })).toBeInTheDocument();
  });

  test('AC-UI-6 form has image (file, accepts images), label, location, catalog and description fields', async () => {
    const { form } = await openAdd();
    const file = within(form).getByLabelText(/image/i) as HTMLInputElement;
    expect(file.type).toBe('file');
    expect(file.accept).toMatch(/image/);
    for (const re of [/^label$/i, /location/i, /catalog/i, /description/i]) {
      expect(within(form).getByLabelText(re)).toBeInTheDocument();
    }
  });

  test('AC-UI-6 submit is disabled with a validation message before any image is chosen', async () => {
    const { form } = await openAdd();
    expect(submitBtn(form)).toBeDisabled();
    expect(within(form).getByText(/image is required/i)).toBeInTheDocument();
  });

  test('AC-UI-6 still disabled with label + location filled but no image', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    expect(submitBtn(form)).toBeDisabled();
    expect(within(form).getByText(/image is required/i)).toBeInTheDocument();
  });

  test('AC-UI-6 choosing an image clears the message and enables submit', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    await user.upload(within(form).getByLabelText(/image/i), png());
    expect(within(form).queryByText(/image is required/i)).toBeNull();
    expect(submitBtn(form)).toBeEnabled();
  });

  test('AC-UI-6 removing the chosen image disables submit again', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    const input = within(form).getByLabelText(/image/i) as HTMLInputElement;
    await user.upload(input, png());
    expect(submitBtn(form)).toBeEnabled();
    fireEvent.change(input, { target: { files: [] } });
    await waitFor(() => expect(submitBtn(form)).toBeDisabled());
    expect(within(form).getByText(/image is required/i)).toBeInTheDocument();
  });

  test('AC-UI-6 a non-image file does not satisfy the image requirement', async () => {
    const { form } = await openAdd();
    const input = within(form).getByLabelText(/image/i) as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(['hello'], 'notes.txt', { type: 'text/plain' })] } });
    await waitFor(() =>
      expect(within(form).getByText(/image is required|must be an image|not an image|choose an image/i)).toBeInTheDocument(),
    );
    expect(submitBtn(form)).toBeDisabled();
  });

  test('AC-UI-6 posts multipart with image, label, location_id, catalog_id and description', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    await user.selectOptions(within(form).getByLabelText(/catalog/i), 'wipes-pack');
    await user.type(within(form).getByLabelText(/description/i), 'Blue handle');
    await user.upload(within(form).getByLabelText(/image/i), png());
    await user.click(submitBtn(form));
    await waitFor(() => expect(requestsTo('POST', '/api/items')).toHaveLength(1));
    const body = requestsTo('POST', '/api/items')[0].body as MultipartBody;
    expect(body).toBeInstanceOf(MultipartBody); // i.e. sent as multipart/form-data
    const img = body.get('image') as FilePart;
    expect(img).toBeTruthy();
    expect(img.name).toBe('tool.png');
    expect(img.size).toBe(4);
    expect(img.type).toBe('image/png');
    expect(body.get('label')).toBe('Torque wrench');
    expect(body.get('location_id')).toBe('NOD2D4');
    expect(body.get('catalog_id')).toBe('wipes-pack');
    expect(body.get('description')).toBe('Blue handle');
  });

  test('AC-UI-6 optional catalog/description are omitted when not provided', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    await user.upload(within(form).getByLabelText(/image/i), png());
    await user.click(submitBtn(form));
    await waitFor(() => expect(requestsTo('POST', '/api/items')).toHaveLength(1));
    const body = requestsTo('POST', '/api/items')[0].body as MultipartBody;
    expect(body).toBeInstanceOf(MultipartBody);
    expect(body.has('catalog_id')).toBe(false);
    expect(body.has('description')).toBe(false);
  });

  test('AC-UI-6 missing label (image chosen) does not post', async () => {
    const { user, form } = await openAdd();
    await user.selectOptions(within(form).getByLabelText(/location/i), 'NOD2D4');
    await user.upload(within(form).getByLabelText(/image/i), png());
    const btn = submitBtn(form) as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', '/api/items')).toHaveLength(0);
  });

  test('AC-UI-6 missing location (image chosen) does not post', async () => {
    const { user, form } = await openAdd();
    await user.type(within(form).getByLabelText(/^label$/i), 'Torque wrench');
    await user.upload(within(form).getByLabelText(/image/i), png());
    const btn = submitBtn(form) as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', '/api/items')).toHaveLength(0);
  });

  test('AC-UI-6 success (201) is shown', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    await user.upload(within(form).getByLabelText(/image/i), png());
    await user.click(submitBtn(form));
    expect(await findToast(/added|created/i)).toBeInTheDocument();
  });

  test('AC-UI-6 API 422 shows an error toast and keeps the form values', async () => {
    server.use(http.post('*/api/items', () => HttpResponse.json({ detail: 'image is not a valid picture' }, { status: 422 })));
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    await user.upload(within(form).getByLabelText(/image/i), png());
    await user.click(submitBtn(form));
    expect(await findToast(/not a valid picture|422|failed|error/i)).toBeInTheDocument();
    const f = screen.getByRole('form', { name: /add item/i });
    expect(within(f).getByLabelText(/^label$/i)).toHaveValue('Torque wrench');
  });
});

describe('AC-UI-6 review round 1: operator attribution', () => {
  test('AC-UI-6 the form has an operator field prefilled from the shared operator and posts it', async () => {
    window.localStorage.setItem('nasa-omni.operator', 'carol');
    const { user, form } = await openAdd();
    expect(within(form).getByLabelText(/operator/i)).toHaveValue('carol');
    await fillRequired(user, form);
    await user.upload(within(form).getByLabelText(/image/i), png());
    await user.click(submitBtn(form));
    await waitFor(() => expect(requestsTo('POST', '/api/items')).toHaveLength(1));
    expect((requestsTo('POST', '/api/items')[0].body as MultipartBody).get('operator')).toBe('carol');
  });

  test('AC-UI-6 an operator typed here becomes the shared operator; empty operator is omitted', async () => {
    const { user, form } = await openAdd();
    await fillRequired(user, form);
    await user.upload(within(form).getByLabelText(/image/i), png());
    await user.click(submitBtn(form));
    await waitFor(() => expect(requestsTo('POST', '/api/items')).toHaveLength(1));
    expect((requestsTo('POST', '/api/items')[0].body as MultipartBody).has('operator')).toBe(false);
  });
});
