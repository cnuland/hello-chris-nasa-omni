import type { Item, ItemPatch } from '../api/types';

export interface EditFormValues {
  label: string;
  description: string;
  catalog_id: string; // '' = none
  last_location: string;
  status: string;
  operator: string;
  note: string;
}

const PATCHABLE_STATUSES = new Set(['confirmed', 'provisional', 'unknown', 'dismissed']);

/** The label is required: a blank label is a validation error, never a silently dropped field. */
export function labelError(values: EditFormValues): string | null {
  return values.label.trim() ? null : 'Label is required.';
}

/** AC-UI-5: PATCH body with only the changed fields (+ operator/note when given). Empty object = nothing changed. */
export function changedFields(original: Item, values: EditFormValues): ItemPatch {
  const patch: ItemPatch = {};

  const label = values.label.trim();
  if (label && label !== original.label) patch.label = label;

  const description = values.description.trim();
  if (description !== (original.description ?? '').trim()) patch.description = description;

  const catalog = values.catalog_id.trim();
  if (catalog !== (original.catalog_id ?? '')) patch.catalog_id = catalog || null;

  const location = values.last_location.trim();
  if (location && location !== (original.last_location ?? '')) patch.last_location = location;

  if (values.status !== original.status && PATCHABLE_STATUSES.has(values.status)) {
    patch.status = values.status as ItemPatch['status'];
  }

  if (Object.keys(patch).length === 0) return {};
  const operator = values.operator.trim();
  const note = values.note.trim();
  if (operator) patch.operator = operator;
  if (note) patch.note = note;
  return patch;
}
