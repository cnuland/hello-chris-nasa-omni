// Render helpers: the full App inside a MemoryRouter, with a probe exposing the current URL.
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { vi } from 'vitest';
import App from '../src/App';

export const current = { pathname: '', search: '' };

function LocationProbe() {
  const loc = useLocation();
  current.pathname = loc.pathname;
  current.search = loc.search;
  return null;
}

export function renderApp(path = '/', opts: { fakeTimers?: boolean } = {}) {
  const user = opts.fakeTimers
    ? userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
    : userEvent.setup();
  const utils = render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <LocationProbe />
    </MemoryRouter>,
  );
  return { user, ...utils };
}

export function searchParams(): URLSearchParams {
  return new URLSearchParams(current.search);
}

/** Toast region (AC-UI-10): role="region" aria-label="Notifications". */
export function toastRegion() {
  return screen.getByRole('region', { name: /notifications/i });
}

export async function findToast(text: RegExp) {
  const region = await screen.findByRole('region', { name: /notifications/i });
  return within(region).findByText(text);
}

/** Inventory item container (row or card): data-testid="item-<entity_id>". */
export async function findItem(id: string) {
  return screen.findByTestId(`item-${id}`);
}

/** Review card: data-testid="review-<review_id>". */
export async function findReviewCard(id: string) {
  return screen.findByTestId(`review-${id}`);
}

/** Use only Date faking (keeps real timers so msw/react work normally). */
export function freezeDate(d: Date) {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(d);
}

/** Full fake timers that still advance with real time so promises/msw resolve. */
export function fakeTimers(d: Date) {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(d);
}
