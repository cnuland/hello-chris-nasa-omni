// AC-UI-24 (integration 2026-10-18): the agent now sends optional label / location / location_name on citations
// (AC-AGT-17) so chips need no extra lookup. The sanitizer kept only ids, dropping them. Labels are length-capped
// plain strings; anything else is ignored. Item citations may also carry their catalog_id.
import { describe, expect, test } from 'vitest';
import { validateChatResponse } from '../src/lib/agent';

const E = '11111111-1111-4111-8111-111111111111';

describe('AC-UI-24 citation labels pass through the sanitizer', () => {
  test('label, location, location_name and catalog_id are kept on entity citations', () => {
    const r = validateChatResponse({ reply: 'x', citations: [
      { entity_id: E, at: '2026-10-17T21:42:00Z', label: 'Laptop', location: 'COL1', location_name: 'Columbus', catalog_id: 'laptop' },
    ] });
    expect(typeof r).not.toBe('string');
    const c = (r as unknown as { citations: Record<string, unknown>[] }).citations[0];
    expect(c).toMatchObject({ entity_id: E, label: 'Laptop', location: 'COL1', location_name: 'Columbus', catalog_id: 'laptop' });
  });

  test('catalog citations keep their label', () => {
    const r = validateChatResponse({ reply: 'x', citations: [{ catalog_id: 'multimeter', label: 'Fluke multimeter' }] });
    expect((r as unknown as { citations: Record<string, unknown>[] }).citations[0]).toMatchObject({ catalog_id: 'multimeter', label: 'Fluke multimeter' });
  });

  test('non-string or oversized labels are dropped, the citation survives', () => {
    const r = validateChatResponse({ reply: 'x', citations: [
      { entity_id: E, label: { html: '<img onerror=x>' }, location_name: 'y'.repeat(500) },
    ] });
    const c = (r as unknown as { citations: Record<string, unknown>[] }).citations[0];
    expect(c.entity_id).toBe(E);
    expect(c.label).toBeUndefined();
    expect(c.location_name).toBeUndefined();
  });
});
