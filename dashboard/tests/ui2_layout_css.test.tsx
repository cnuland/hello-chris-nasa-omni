// UI review round 2, layout (jsdom does no layout, so this checks the CSS rules and DOM grouping; the real pixel
// measurements at 1024 and 1440 px are taken with Playwright, see docs/tdd-log/dashboard.md "UI review round 2 fixes"):
//   UI2-17 / UI-21 filter bar: fields top-aligned (the date hint no longer lifts the date inputs), "Needs review" grouped
//   with Status, Sort wide enough for "Last seen (newest first)"; 1024 px duplicate cards: sticky actions footer;
//   UI2-07 box styles: detector solid, omni dashed with an "approximate" tag.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { screen, within } from '@testing-library/react';
import { describe, expect, test } from 'vitest';
import { renderApp } from './render';

const css = readFileSync(resolve(__dirname, '../src/styles/theme.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** Declarations of every rule whose selector list contains `selector` exactly (outside media queries too). */
function decls(selector: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /([^{}]+)\{([^{}]*)\}/g;
  for (const m of css.matchAll(re)) {
    const sels = m[1].split(',').map((x) => x.trim());
    if (!sels.includes(selector)) continue;
    for (const d of m[2].split(';')) {
      const i = d.indexOf(':');
      if (i > 0) out[d.slice(0, i).trim()] = d.slice(i + 1).trim();
    }
  }
  return out;
}

describe('UI2-07 box styles', () => {
  test('detector boxes are solid; omni boxes are dashed with a visible tag', () => {
    expect(decls('.bbox-approx')['border-style']).toBe('dashed');
    expect(decls('.bbox-detector')['border-style'] ?? 'solid').toBe('solid');
    // Amended after the axe run: the tag is generated content, not a text node inside the enlarge button.
    expect(decls('.bbox-approx::after')).toHaveProperty('position', 'absolute');
    expect(decls('.bbox-approx::after').content).toBe("'approximate'");
  });
});

describe('1024 px duplicate cards: actions footer', () => {
  test('the duplicate-card footer is sticky at the bottom of the viewport with an opaque background', () => {
    const f = decls('.review-footer');
    expect(f.position).toBe('sticky');
    expect(f.bottom).toBe('0');
    expect(f.background ?? f['background-color']).toBeTruthy();
  });
});

describe('UI2-17 / UI-21 filter bar', () => {
  test('filter fields are top-aligned so the date hint does not lift the date inputs', () => {
    expect(decls('.filters')['align-items']).toBe('start');
  });

  test('the Sort select is at least 14rem wide (no clipping at 1024 px)', () => {
    expect(decls('.sort-field select')['min-width']).toBe('14rem');
  });

  test('"Needs review" sits inside the Status group', async () => {
    renderApp('/');
    const status = await screen.findByRole('group', { name: /^status$/i });
    expect(within(status).getByRole('checkbox', { name: /needs review/i })).toBeInTheDocument();
  });
});
