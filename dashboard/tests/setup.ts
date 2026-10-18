// Global test setup: jest-dom matchers, msw lifecycle, and a fetch shim that mimics browser behaviour in jsdom.
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { encodeMultipart } from './multipart';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { resetRequests, server } from './server';
import { clearItemCache } from '../src/state/itemCache';

process.env.TZ = 'UTC';

// Node's fetch (undici) does not resolve relative URLs the way a browser does, and cannot serialise FormData under
// the jsdom realm. This shim (test infrastructure only) resolves URLs against window.location and pre-encodes
// FormData bodies as multipart bytes, so app code can call fetch('/api/...', {body: formData}) as in a browser.
function installFetchShim() {
  const inner = globalThis.fetch;
  const shim = async (input: RequestInfo | URL, init?: RequestInit) => {
    if (typeof input === 'string' && !/^[a-z]+:/i.test(input)) input = new URL(input, window.location.href).toString();
    if (init?.body && init.body instanceof FormData) {
      const { body, contentType } = await encodeMultipart(init.body);
      const headers = new Headers(init.headers);
      headers.set('content-type', contentType);
      init = { ...init, headers, body: body as unknown as BodyInit };
    }
    return inner(input, init);
  };
  globalThis.fetch = shim as typeof fetch;
  window.fetch = shim as typeof fetch;
}

// jsdom gaps used by typical UI code
if (!window.matchMedia) {
  window.matchMedia = (q: string) =>
    ({ matches: false, media: q, onchange: null, addListener() {}, removeListener() {}, addEventListener() {},
      removeEventListener() {}, dispatchEvent: () => false }) as unknown as MediaQueryList;
}
if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = function () {};
if (!URL.createObjectURL) URL.createObjectURL = () => 'blob:preview';
if (!URL.revokeObjectURL) URL.revokeObjectURL = () => {};

beforeAll(() => {
  server.listen({ onUnhandledFrame: 'error' });
  installFetchShim();
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetRequests();
  vi.useRealTimers();
  window.localStorage.clear();
  window.sessionStorage.clear();
  clearItemCache();
});
afterAll(() => server.close());
