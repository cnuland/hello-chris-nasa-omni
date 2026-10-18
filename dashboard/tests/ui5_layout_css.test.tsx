// UI review round 5: the bulk-select checkbox widens the review card header. At 1440 px (two-column cards) Playwright
// showed "UNKNOWN OBJECTObserved" run together and "History" pushed under it. jsdom does no layout, so this pins the
// rule; the pixel check is scripts/ui_check.py `round5`.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, test } from 'vitest';

const css = readFileSync(resolve(__dirname, '../src/styles/theme.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

function decls(selector: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const m of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (!m[1].split(',').map((x) => x.trim()).includes(selector)) continue;
    for (const d of m[2].split(';')) {
      const i = d.indexOf(':');
      if (i > 0) out[d.slice(0, i).trim()] = d.slice(i + 1).trim();
    }
  }
  return out;
}

describe('UI5-02 card header with the bulk checkbox', () => {
  test('the header wraps with a gap between the title and the meta', () => {
    const head = decls('.review-head');
    expect(head['flex-wrap']).toBe('wrap');
    expect(head.gap).toMatch(/^\d+px \d+px$|^\d+px$/);
  });

  test('the shared-frame note and bulk list wrap long labels', () => {
    expect(decls('.same-frame-note')['overflow-wrap']).toBe('anywhere');
    expect(decls('.bulk-list')['overflow-wrap']).toBe('anywhere');
  });
});
