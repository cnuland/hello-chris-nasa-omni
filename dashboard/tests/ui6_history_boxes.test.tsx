// UI6-01 (round 6, live): history frames drew no box although the API has a bbox for every public object sighting.
// ItemDetailPage keyed the sighting map on `typeof sighting_id === 'string'`, but the detail API returns integer
// sighting ids (and history entries reference them as integers), so no sighting was ever found. Ids are now
// normalised to strings on both sides.
import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, test } from 'vitest';
import type { ItemDetail, TimelineEntry } from '../src/api/types';
import { drill } from './fixtures';
import { server } from './server';
import { renderApp } from './render';

const LAPTOP = '7a7a7a7a-1111-4222-8333-444444444444';

describe('UI6-01 history frames keep their boxes with integer sighting ids', () => {
  test('a history sighting whose id is an integer draws the API box', async () => {
    const item = { ...drill, entity_id: LAPTOP, label: 'laptop', events: [],
      sightings: [{ sighting_id: 42, seen_at: '2026-10-18T08:01:00Z', frame_url: '/media/frames/080100.jpg',
        bbox: [0.1, 0.2, 0.3, 0.4], bbox_source: 'detector' }] } as unknown as ItemDetail;
    const hist = [{ at: '2026-10-18T08:01:00Z', kind: 'sighting', id: 42, summary: 'Seen at LAB1',
      image_url: '/media/frames/080100.jpg' }] as unknown as TimelineEntry[];
    server.use(
      http.get(`*/api/items/${LAPTOP}`, () => HttpResponse.json(item)),
      http.get(`*/api/items/${LAPTOP}/history`, () => HttpResponse.json(hist)),
    );
    renderApp(`/items/${LAPTOP}`);
    const histList = await screen.findByRole('list', { name: /^history$/i });
    await within(histList).findByRole('button', { name: /enlarge image/i });
    expect(histList.querySelector('[data-testid="bbox"]')).not.toBeNull();
  });

  test('string ids keep working', async () => {
    const item = { ...drill, entity_id: LAPTOP, label: 'laptop', events: [],
      sightings: [{ sighting_id: 's9', seen_at: '2026-10-18T08:01:00Z', frame_url: '/media/frames/080100.jpg',
        bbox: [0.1, 0.2, 0.3, 0.4], bbox_source: 'omni' }] } as unknown as ItemDetail;
    const hist = [{ at: '2026-10-18T08:01:00Z', kind: 'sighting', id: 's9', summary: 'Seen at LAB1',
      image_url: '/media/frames/080100.jpg' }] as unknown as TimelineEntry[];
    server.use(
      http.get(`*/api/items/${LAPTOP}`, () => HttpResponse.json(item)),
      http.get(`*/api/items/${LAPTOP}/history`, () => HttpResponse.json(hist)),
    );
    renderApp(`/items/${LAPTOP}`);
    const histList = await screen.findByRole('list', { name: /^history$/i });
    await within(histList).findByRole('button', { name: /enlarge image/i });
    expect(histList.querySelector('[data-testid="bbox"]')).not.toBeNull();
  });
});
