// UI review round 4, UI4-02: wide crops overflowed the review card (document 1890 px wide at 1440, 1186 px at 1024).
// jsdom does no layout, so these tests pin the CSS rules that keep every image inside its container; the pixel check
// (no horizontal scroll, Classify / Dismiss on screen at 1024×768 and 1440×1000) is scripts/ui_check.py.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, test } from 'vitest';

const css = readFileSync(resolve(__dirname, '../src/styles/theme.css'), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');

/** Declarations of every top-level rule whose selector list contains `selector` (later rules win). */
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

describe('UI4-02 images never exceed their container', () => {
  test('every frame trigger (review, candidate tile, hero) is capped at its container width', () => {
    expect(decls('.frame-trigger')['max-width']).toBe('100%');
    expect(decls('.frame-trigger')['min-width']).toBe('0');
  });

  test('images inside frames scale down with height auto', () => {
    const img = decls('.frame img');
    expect(img['max-width']).toBe('100%');
    expect(img.height).toBe('auto');
    const crop = decls('.frame-crop img');
    expect(crop['max-width']).toBe('100%');
    expect(crop.height).toBe('auto');
  });

  test('the unknown-card media column cannot grow to the crop\'s natural width', () => {
    const body = decls('.review-body');
    expect(body['grid-template-columns']).toMatch(/^minmax\(0, (?:\d+px|max-content|auto)\) minmax\(0, 1fr\)$/);
    expect(body['grid-template-columns']).not.toMatch(/^max-content/);
    expect(decls('.review-media')['min-width']).toBe('0');
    expect(decls('.review-media')['max-width']).toMatch(/^\d+px$/);
  });

  test('candidate tiles and review cards shrink instead of overflowing', () => {
    expect(decls('.candidate')['min-width']).toBe('0');
    expect(decls('.candidate')['max-width']).toBe('100%');
    expect(decls('.review-card')['min-width']).toBe('0');
    expect(decls('.review-info')['min-width']).toBe('0');
  });

  test('the item detail hero (frame + inset close-up) stays in its column', () => {
    expect(decls('.detail-media')['min-width']).toBe('0');
    expect(decls('.hero-inset')['max-width']).toBe('100%');
  });

  test('small crops are still never upscaled by CSS (the 2× rule is applied in code)', () => {
    expect(decls('.frame-crop img').width).toBe('auto');
  });

  // cycle 2 (found by Playwright): an appended `.frame-trigger { max-width: 100% }` overrode `.frame-lg`'s 640 px cap,
  // and `min-width: 0` let the inset close-up shrink below its natural size in the flex row.
  test('the generic frame cap comes before the size rules, so .frame-lg keeps its 640 px cap', () => {
    const generic = [...css.matchAll(/(^|\})\s*\.frame-trigger\s*\{[^}]*max-width/g)].map((m) => m.index ?? 0);
    expect(generic.length).toBeGreaterThan(0);
    expect(Math.max(...generic)).toBeLessThan(css.indexOf('.frame-lg {'));
    expect(decls('.frame-lg')['max-width']).toBe('640px');
  });

  test('the inset close-up keeps its natural size (no flex shrink)', () => {
    expect(decls('.hero-inset .frame-trigger').flex).toBe('none');
  });
});
