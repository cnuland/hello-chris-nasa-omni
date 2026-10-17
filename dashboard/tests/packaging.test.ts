// Packaging: dev proxy (/api -> :8080, /agent -> :8090), nginx Containerfile with env-configurable proxies.
import { existsSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, test } from 'vitest';
import config from '../vite.config';

const ROOT = resolve(__dirname, '..');
type Proxy = { target: string; rewrite?: (p: string) => string };

describe('packaging (spec header: talks only to /api and /agent)', () => {
  test('dev proxy /api -> http://localhost:8080 with the prefix stripped', () => {
    const p = (config as { server: { proxy: Record<string, Proxy> } }).server.proxy['/api'];
    expect(p.target).toBe('http://localhost:8080');
    expect(p.rewrite!('/api/items?q=x')).toBe('/items?q=x');
  });

  test('dev proxy /agent -> http://localhost:8090 with the prefix stripped', () => {
    const p = (config as { server: { proxy: Record<string, Proxy> } }).server.proxy['/agent'];
    expect(p.target).toBe('http://localhost:8090');
    expect(p.rewrite!('/agent/chat/stream')).toBe('/chat/stream');
  });

  test('Containerfile builds with node and serves dist/ from unprivileged nginx on 8080', () => {
    const cf = readFileSync(join(ROOT, 'Containerfile'), 'utf8');
    expect(cf).toMatch(/FROM\s+\S*node:\S+\s+AS\s+build/i);
    expect(cf).toMatch(/npm (ci|install)/);
    expect(cf).toMatch(/npm run build/);
    expect(cf).toMatch(/FROM\s+\S*nginx/i);
    expect(cf).toMatch(/\/app\/dist/);
    expect(cf).toMatch(/EXPOSE\s+8080/);
    expect(cf).toMatch(/API_UPSTREAM=/);
    expect(cf).toMatch(/AGENT_UPSTREAM=/);
  });

  test('nginx template proxies /api and /agent to env-configured upstreams, SPA fallback, unbuffered SSE', () => {
    const tpl = readFileSync(join(ROOT, 'nginx', 'default.conf.template'), 'utf8');
    expect(tpl).toMatch(/location \/api\/ \{[^}]*proxy_pass \$\{API_UPSTREAM\}\/;/s);
    expect(tpl).toMatch(/location \/agent\/ \{[^}]*proxy_pass \$\{AGENT_UPSTREAM\}\/;[^}]*proxy_buffering off;/s);
    expect(tpl).toMatch(/try_files \$uri \$uri\/ \/index\.html;/);
    expect(tpl).toMatch(/listen\s+8080/);
  });

  test('nginx sends security headers (CSP, nosniff, frame-ancestors, referrer policy) and no-cache for index.html', () => {
    const tpl = readFileSync(join(ROOT, 'nginx', 'default.conf.template'), 'utf8');
    const csp = tpl.match(/add_header\s+Content-Security-Policy\s+"([^"]+)"\s+always;/);
    expect(csp, 'Content-Security-Policy header').not.toBeNull();
    const policy = csp![1];
    expect(policy).toMatch(/default-src 'self'/);
    expect(policy).toMatch(/img-src 'self' blob: data:/);
    expect(policy).toMatch(/connect-src 'self'/);
    expect(policy).toMatch(/frame-ancestors 'none'/);
    expect(policy).not.toMatch(/unsafe-inline|unsafe-eval|\*/);
    expect(tpl).toMatch(/add_header\s+X-Content-Type-Options\s+"?nosniff"?\s+always;/);
    expect(tpl).toMatch(/add_header\s+X-Frame-Options\s+"?DENY"?\s+always;/);
    expect(tpl).toMatch(/add_header\s+Referrer-Policy\s+"?[a-z-]+"?\s+always;/);
    // index.html must never be cached (new bundle after a redeploy); hashed assets may be cached long-term
    expect(tpl).toMatch(/location = \/index\.html \{[^}]*Cache-Control\s+"no-cache[^"]*"/s);
    expect(tpl).toMatch(/location \/assets\/ \{[^}]*Cache-Control\s+"[^"]*immutable[^"]*"/s);
  });

  test('nginx security headers are repeated in every location that sets add_header (nginx does not inherit them)', () => {
    const tpl = readFileSync(join(ROOT, 'nginx', 'default.conf.template'), 'utf8');
    const blocks = [...tpl.matchAll(/location [^{]+\{([^}]*)\}/gs)].map((m) => m[1]);
    for (const b of blocks.filter((x) => /add_header/.test(x))) {
      expect(b).toMatch(/Content-Security-Policy/);
      expect(b).toMatch(/X-Content-Type-Options/);
    }
  });

  test('no secrets or hard-coded cluster URLs in source', () => {
    const src = ['src/api/client.ts', 'vite.config.ts', 'Containerfile'].map((f) =>
      existsSync(join(ROOT, f)) ? readFileSync(join(ROOT, f), 'utf8') : '',
    ).join('\n');
    expect(src).not.toMatch(/(password|secret|api_?key|token)\s*[:=]\s*['"][^'"]+['"]/i);
    expect(src).not.toMatch(/Bearer\s+[A-Za-z0-9]/);
    expect(src).not.toMatch(/\.svc(\.cluster\.local)?:/);
  });
});
