// AC-UI-10 Accessible labels on all inputs; works at 1280 px and 1920 px; dark mission-control theme; API errors are
// non-blocking toasts.
import { screen, waitFor, within } from '@testing-library/react';
import { computeAccessibleName } from 'dom-accessibility-api';
import { http, HttpResponse } from 'msw';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, test } from 'vitest';
import { ID } from './fixtures';
import { server } from './server';
import { findItem, findReviewCard, findToast, renderApp } from './render';

const ROOT = resolve(__dirname, '..');

function controls(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll<HTMLElement>('input:not([type=hidden]), select, textarea'),
  );
}

function expectAllLabelled(min: number) {
  const els = controls();
  expect(els.length).toBeGreaterThanOrEqual(min);
  const unlabelled = els.filter((el) => computeAccessibleName(el).trim() === '').map((el) => el.outerHTML.slice(0, 120));
  expect(unlabelled).toEqual([]);
  const buttons = Array.from(document.querySelectorAll<HTMLElement>('button'));
  const unnamed = buttons.filter((b) => computeAccessibleName(b).trim() === '').map((b) => b.outerHTML.slice(0, 120));
  expect(unnamed).toEqual([]);
}

function cssFiles(dir: string): string[] {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? cssFiles(p) : p.endsWith('.css') ? [p] : [];
  });
}

function luminance(hex: string): number {
  let h = hex.replace('#', '');
  if (h.length === 3) h = h.split('').map((c) => c + c).join('');
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((c) =>
    c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4,
  );
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

describe('AC-UI-10 accessible labels on all inputs', () => {
  test('AC-UI-10 inventory page: every input/select/textarea and button has an accessible name', async () => {
    renderApp('/');
    await findItem(ID.drill);
    expectAllLabelled(8); // search, kind, 5 status, location, min confidence, seen after/before, needs review, sort
  });

  test('AC-UI-10 add item page', async () => {
    renderApp('/items/new');
    await screen.findByRole('form', { name: /add item/i });
    expectAllLabelled(5);
  });

  test('AC-UI-10 item detail with the edit form open', async () => {
    const { user } = renderApp(`/items/${ID.drill}`);
    await screen.findByRole('heading', { name: /pistol-grip drill/i });
    await user.click(screen.getByRole('button', { name: /^edit/i }));
    await screen.findByRole('form', { name: /edit item/i });
    expectAllLabelled(7);
  });

  test('AC-UI-10 review page', async () => {
    renderApp('/review');
    await findReviewCard(ID.rvUnknown);
    expectAllLabelled(3);
  });

  test('AC-UI-10 chat page', async () => {
    renderApp('/chat');
    await screen.findByRole('log');
    expectAllLabelled(1);
  });
});

describe('AC-UI-10 layout and navigation', () => {
  test('AC-UI-10 primary navigation links to Inventory, Overview, Review and Chat', async () => {
    renderApp('/');
    const nav = await screen.findByRole('navigation');
    for (const [name, href] of [[/inventory/i, '/'], [/overview/i, '/overview'], [/review|unknowns/i, '/review'], [/chat/i, '/chat']] as const) {
      expect(within(nav).getByRole('link', { name })).toHaveAttribute('href', href);
    }
  });

  // Smoke test only: jsdom does no layout, so this proves the shell mounts with these widths set. The real layout
  // guarantee is the stylesheet width scan below (a Playwright layout check could be added later).
  test.each([1280, 1920])('AC-UI-10 smoke: the app shell mounts with window.innerWidth = %i', async (w) => {
    window.innerWidth = w;
    window.dispatchEvent(new Event('resize'));
    renderApp('/');
    expect(await screen.findByRole('navigation')).toBeInTheDocument();
    expect(await screen.findByRole('main')).toBeInTheDocument();
    await findItem(ID.drill);
  });

  test('AC-UI-10 index.html declares a responsive viewport', () => {
    const html = readFileSync(join(ROOT, 'index.html'), 'utf8');
    expect(html).toMatch(/<meta name="viewport" content="width=device-width/);
  });

  test('AC-UI-10 stylesheets exist and have no fixed widths/min-widths above 1280 px', () => {
    const files = cssFiles(join(ROOT, 'src'));
    expect(files.length).toBeGreaterThan(0);
    for (const f of files) {
      const css = readFileSync(f, 'utf8');
      for (const m of css.matchAll(/(?<![-\w])(min-width|width)\s*:\s*(\d+)px/g)) {
        expect(Number(m[2]), `${f}: ${m[0]}`).toBeLessThanOrEqual(1280);
      }
    }
  });
});

describe('AC-UI-10 dark mission-control theme', () => {
  const themePath = join(ROOT, 'src', 'styles', 'theme.css');

  test('AC-UI-10 src/styles/theme.css declares color-scheme: dark and is imported by main.tsx', () => {
    expect(existsSync(themePath)).toBe(true);
    const css = readFileSync(themePath, 'utf8');
    expect(css).toMatch(/color-scheme\s*:\s*dark/);
    expect(readFileSync(join(ROOT, 'src', 'main.tsx'), 'utf8')).toMatch(/import\s+['"]\.\/styles\/theme\.css['"]/);
  });

  test('AC-UI-10 theme defines a dark background (--bg) and light text (--fg) with strong contrast', () => {
    const css = existsSync(themePath) ? readFileSync(themePath, 'utf8') : '';
    const bg = css.match(/--bg\s*:\s*(#[0-9a-fA-F]{3,6})\b/)?.[1];
    const fg = css.match(/--fg\s*:\s*(#[0-9a-fA-F]{3,6})\b/)?.[1];
    expect(bg, '--bg hex colour').toBeDefined();
    expect(fg, '--fg hex colour').toBeDefined();
    expect(luminance(bg!)).toBeLessThan(0.05);
    expect(luminance(fg!)).toBeGreaterThan(0.6);
    expect((luminance(fg!) + 0.05) / (luminance(bg!) + 0.05)).toBeGreaterThanOrEqual(7);
  });

  test('AC-UI-10 no paid UI kits in dependencies', () => {
    const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));
    const deps = Object.keys({ ...pkg.dependencies, ...pkg.devDependencies });
    for (const banned of ['@mui/x-data-grid-pro', '@mui/x-data-grid-premium', 'ag-grid-enterprise', '@devexpress/dx-react-grid', 'kendo-react-grid']) {
      expect(deps).not.toContain(banned);
    }
  });
});

describe('AC-UI-10 API errors are non-blocking toasts', () => {
  test('AC-UI-10 inventory load failure -> toast in the Notifications region; filters remain usable; no modal', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.json({ detail: 'database unavailable' }, { status: 500 })));
    const { user } = renderApp('/');
    expect(await findToast(/database unavailable|error|failed/i)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.queryByRole('alertdialog')).toBeNull();
    const search = screen.getByLabelText(/search/i);
    expect(search).toBeEnabled();
    await user.type(search, 'x');
    expect(search).toHaveValue('x');
  });

  test('AC-UI-10 the toast shows the API detail message', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.json({ detail: 'database unavailable' }, { status: 500 })));
    renderApp('/');
    expect(await findToast(/database unavailable/)).toBeInTheDocument();
  });

  test('AC-UI-10 network failure (fetch rejects) -> toast, no crash', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.error()));
    renderApp('/');
    expect(await findToast(/network|failed|error/i)).toBeInTheDocument();
    expect(screen.getByRole('navigation')).toBeInTheDocument();
  });

  test('AC-UI-10 toasts can be dismissed', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.json({ detail: 'database unavailable' }, { status: 500 })));
    const { user } = renderApp('/');
    const toast = await findToast(/database unavailable/);
    const region = screen.getByRole('region', { name: /notifications/i });
    const close = within(region).getAllByRole('button', { name: /dismiss|close/i })[0];
    await user.click(close);
    await waitFor(() => expect(toast).not.toBeInTheDocument());
  });

  test('AC-UI-10 error toasts are announced (role=alert inside the Notifications region)', async () => {
    server.use(http.get('*/api/items', () => HttpResponse.json({ detail: 'database unavailable' }, { status: 500 })));
    renderApp('/');
    await findToast(/database unavailable/);
    const region = screen.getByRole('region', { name: /notifications/i });
    expect(within(region).getByRole('alert')).toHaveTextContent(/database unavailable/);
  });
});
