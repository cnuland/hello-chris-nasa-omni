// AC-UI-5: edit an item off-screen. Sends PATCH with only the changed fields (see lib/diff.changedFields).
import { useState, type FormEvent } from 'react';
import { patchItem } from '../api/client';
import type { CatalogItem, Item, ItemPatch } from '../api/types';
import { changedFields, labelError, type EditFormValues } from '../lib/diff';
import type { LocationOption } from '../lib/locations';
import { useOperator } from '../state/operator';
import { useToasts } from '../state/toasts';
import { LocationOptions } from './LocationSelect';

const EDITABLE_STATUSES = ['confirmed', 'provisional', 'unknown', 'dismissed'] as const;

interface Props {
  item: Item;
  catalog: CatalogItem[];
  locations: LocationOption[];
  onSaved: (item: Item, patch: ItemPatch) => void;
  onCancel: () => void;
}

export function initialValues(item: Item, operator = ''): EditFormValues {
  return {
    label: item.label,
    description: item.description ?? '',
    catalog_id: item.catalog_id ?? '',
    last_location: item.last_location ?? '',
    status: item.status,
    operator,
    note: '',
  };
}

export function EditItemForm({ item, catalog, locations, onSaved, onCancel }: Props) {
  const toasts = useToasts();
  const [sharedOperator, setSharedOperator] = useOperator();
  const [values, setValues] = useState<EditFormValues>(() => initialValues(item, sharedOperator.trim()));
  const [saving, setSaving] = useState(false);
  const patch = changedFields(item, values);
  const labelMsg = labelError(values);
  const dirty = Object.keys(patch).length > 0 && !labelMsg;

  const set = (k: keyof EditFormValues) => (e: { target: { value: string } }) => {
    const value = e.target.value;
    setValues((v) => ({ ...v, [k]: value }));
    if (k === 'operator') setSharedOperator(value);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!dirty || saving) return;
    setSaving(true);
    try {
      const updated = await patchItem(item.entity_id, patch);
      toasts.success('Item saved');
      onSaved(updated, patch);
    } catch (err) {
      toasts.error('Could not save item', err);
    } finally {
      setSaving(false);
    }
  };

  // A catalog id not present in the list (e.g. stale) is still shown so the current value round-trips.
  const catalogKnown = !item.catalog_id || catalog.some((c) => c.catalog_id === item.catalog_id);
  const locationKnown = !item.last_location || locations.some((l) => l.location_id === item.last_location);

  return (
    <form className="card form" aria-label="Edit item" noValidate onSubmit={submit}>
      <h2>Edit item</h2>
      <div className="grid2">
        <div className="field">
          <label htmlFor="e-label">Label</label>
          <input id="e-label" value={values.label} required aria-invalid={!!labelMsg}
            aria-describedby={labelMsg ? 'e-label-err' : undefined} onChange={set('label')} />
          {labelMsg && <p id="e-label-err" className="field-error">{labelMsg}</p>}
        </div>
        <div className="field">
          <label htmlFor="e-status">Status</label>
          <select id="e-status" value={values.status} onChange={set('status')}>
            {!EDITABLE_STATUSES.includes(item.status as (typeof EDITABLE_STATUSES)[number]) && (
              <option value={item.status} disabled>{item.status}</option>
            )}
            {EDITABLE_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="e-catalog">Catalog</label>
          <select id="e-catalog" value={values.catalog_id} onChange={set('catalog_id')}>
            <option value="">None</option>
            {!catalogKnown && <option value={item.catalog_id!}>{item.catalog_name || item.catalog_id}</option>}
            {catalog.map((c) => <option key={c.catalog_id} value={c.catalog_id}>{c.display_name}</option>)}
          </select>
        </div>
        <div className="field">
          <label htmlFor="e-location">Location</label>
          <select id="e-location" value={values.last_location} onChange={set('last_location')}>
            {!item.last_location && <option value="">Not set</option>}
            {!locationKnown && <option value={item.last_location!}>{item.last_location_name || item.last_location}</option>}
            <LocationOptions options={locations} />
          </select>
        </div>
      </div>
      <div className="field">
        <label htmlFor="e-desc">Description</label>
        <textarea id="e-desc" rows={2} value={values.description} onChange={set('description')} />
      </div>
      <div className="grid2">
        <div className="field">
          <label htmlFor="e-operator">Operator</label>
          <input id="e-operator" value={values.operator} autoComplete="name" onChange={set('operator')} />
        </div>
        <div className="field">
          <label htmlFor="e-note">Note</label>
          <input id="e-note" value={values.note} onChange={set('note')} />
        </div>
      </div>
      <div className="actions">
        <button type="submit" className="primary" disabled={!dirty || saving}>{saving ? 'Saving…' : 'Save'}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
