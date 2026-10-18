// UI review round 3, layout rules (jsdom does no layout; the pixel checks at 1024×768 and 1440×1000 are done with
// Playwright, scripts/ui_check.py, see docs/tdd-log/dashboard.md "UI review round 3 fixes"):
//   UI3-09 the duplicate-card footer is one compact flex row (at most 64 px tall in the browser);
//   UI3-07 crops render at natural size with object-fit: contain (never stretched to the frame width).
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

describe('UI3-09 compact duplicate footer', () => {
  test('the footer row is a single flex row of buttons', () => {
    const r = decls('.review-footer-row');
    expect(r.display).toBe('flex');
    expect(r['align-items']).toBe('center');
  });

  test('the footer padding is small (the round-2 footer was 171 px)', () => {
    const f = decls('.review-footer');
    expect(f.position).toBe('sticky');
    const [top, , bottom = top] = (f.padding ?? '').split(/\s+/).map((v) => parseFloat(v));
    expect(top).toBeLessThanOrEqual(8);
    expect(bottom).toBeLessThanOrEqual(8);
  });

  test('the keep-both hint is screen-reader text inside the footer, not a visible extra line', () => {
    expect(decls('.review-footer .keep-hint')).toMatchObject({ position: 'absolute' });
  });
});

describe('UI3-09 1024 px: subject and candidates side by side', () => {
  test('between 861 and 1100 px the duplicate compare keeps a narrow subject column next to the candidates', () => {
    const block = /@media\s*\(max-width:\s*1100px\)\s*and\s*\(min-width:\s*861px\)\s*\{([\s\S]*?\})\s*\}/.exec(css);
    expect(block).not.toBeNull();
    const rule = /\.dup-compare\s*\{([^}]*)\}/.exec(block![1]);
    expect(rule?.[1]).toMatch(/grid-template-columns:\s*200px minmax\(0, 1fr\)/);
    // the side-by-side block must come after the round-2 1100 px rule that stacks them
    expect(css.lastIndexOf('and (min-width: 861px)')).toBeGreaterThan(css.lastIndexOf('.dup-compare { grid-template-columns: minmax(0, 1fr); }'));
  });
});

describe('UI3-07 crops at natural size', () => {
  test('the crop trigger is not stretched and the image keeps its natural size', () => {
    expect(decls('.frame-trigger.frame-crop').width).toBe('auto');
    const img = decls('.frame-crop img');
    expect(img.width).toBe('auto');
    expect(img['max-width']).toBe('100%');
    expect(img['object-fit']).toBe('contain');
  });
});
