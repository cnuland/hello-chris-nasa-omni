// Review round 1: a render error in one page must not unmount the whole SPA (nav + toasts stay usable).
import { render, screen, within } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { renderApp } from './render';
import { ErrorBoundary } from '../src/components/ErrorBoundary';

vi.mock('../src/pages/OverviewPage', () => ({
  default: () => {
    throw new Error('boom from overview');
  },
}));

function Thrower(): JSX.Element {
  throw new Error('kaput');
}

describe('ErrorBoundary (review round 1, AC-UI-10 robustness)', () => {
  test('a page that throws during render keeps the shell; the error panel is shown and other pages still work', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      const { user } = renderApp('/overview');
      const panel = await screen.findByRole('alert');
      expect(panel).toHaveTextContent(/something went wrong|could not be displayed/i);
      const nav = screen.getByRole('navigation');
      expect(nav).toBeInTheDocument();
      expect(screen.getByRole('region', { name: /notifications/i })).toBeInTheDocument();
      // navigating away resets the boundary
      await user.click(within(nav).getByRole('link', { name: /chat/i }));
      expect(await screen.findByLabelText(/message/i)).toBeInTheDocument();
      expect(screen.queryByText(/something went wrong|could not be displayed/i)).toBeNull();
    } finally {
      spy.mockRestore();
    }
  });

  test('ErrorBoundary renders a fallback for a throwing child', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      render(<ErrorBoundary><Thrower /></ErrorBoundary>);
      expect(screen.getByRole('alert')).toHaveTextContent(/kaput/);
    } finally {
      spy.mockRestore();
    }
  });
});
