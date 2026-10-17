// msw server with default handlers for every endpoint the dashboard may call, plus a request log.
import { setupServer } from 'msw/node';
import { decodeMultipart } from './multipart';
import { http, HttpResponse } from 'msw';
import { CATALOG, ITEMS, LOCATIONS, RECENT_EVENTS, REVIEWS, STATS, drillDetail, drillHistory, ID, crew } from './fixtures';

export interface LoggedRequest {
  method: string;
  url: URL;
  path: string; // pathname, e.g. /api/items
  body: unknown; // parsed JSON, MultipartBody (tests/multipart.ts), text or undefined
}

export const requests: LoggedRequest[] = [];

export function resetRequests() {
  requests.length = 0;
}

export function requestsTo(method: string, path: string | RegExp): LoggedRequest[] {
  return requests.filter(
    (r) => r.method === method && (typeof path === 'string' ? r.path === path : path.test(r.path)),
  );
}

/** Latest GET /api/items request (the list call, not /items/{id}). */
export function lastItemsQuery(): URLSearchParams | undefined {
  const list = requestsTo('GET', '/api/items');
  return list.length ? list[list.length - 1].url.searchParams : undefined;
}

/** Logs a request once (keyed by msw requestId); the body is parsed before any handler consumes it. */
const seen = new Set<string>();
async function log(request: Request, requestId: string) {
  if (seen.has(requestId)) return;
  seen.add(requestId);
  const url = new URL(request.url);
  let body: unknown;
  // clone synchronously, before any handler consumes the body
  const clone = ['GET', 'HEAD'].includes(request.method) ? null : request.clone();
  if (clone) {
    const ct = request.headers.get('content-type') ?? '';
    try {
      const buf = new Uint8Array(await clone.arrayBuffer());
      if (ct.includes('application/json')) body = JSON.parse(new TextDecoder().decode(buf));
      else if (ct.includes('multipart/form-data')) body = decodeMultipart(buf, ct);
      else body = new TextDecoder().decode(buf);
    } catch {
      body = undefined;
    }
  }
  requests.push({ method: request.method, url, path: url.pathname, body });
}

/** Fall-through handler that logs; always kept first (see server.use patch below). */
const logger = http.all('*', async ({ request, requestId }) => {
  await log(request, requestId);
});

export const defaultHandlers = [
  logger,
  http.get('*/api/items', ({ request }) => {
    const u = new URL(request.url);
    if (u.searchParams.get('kind') === 'person') {
      return HttpResponse.json({ items: [crew], total: 2 });
    }
    return HttpResponse.json({ items: ITEMS, total: ITEMS.length });
  }),
  http.get(`*/api/items/${ID.drill}`, () => HttpResponse.json(drillDetail)),
  http.get(`*/api/items/${ID.drill}/history`, () => HttpResponse.json(drillHistory)),
  http.get('*/api/items/:id/history', () => HttpResponse.json([])),
  http.get('*/api/items/:id', () => HttpResponse.json({ detail: 'not found' }, { status: 404 })),
  http.patch('*/api/items/:id', async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...drillDetail, ...body, entity_id: params.id });
  }),
  http.post('*/api/items', () =>
    HttpResponse.json(
      { ...drillDetail, entity_id: ID.newItem, label: 'New item', status: 'confirmed', snapshot_url: '/media/snap/new.jpg' },
      { status: 201 },
    ),
  ),
  http.get('*/api/review', () => HttpResponse.json(REVIEWS)),
  http.post('*/api/review/:id/resolve', () => HttpResponse.json({ status: 'resolved' })),
  http.get('*/api/catalog', () => HttpResponse.json(CATALOG)),
  http.post('*/api/catalog', async ({ request }) => HttpResponse.json(await request.json(), { status: 201 })),
  http.get('*/api/locations', () => HttpResponse.json(LOCATIONS)),
  http.get('*/api/events', () => HttpResponse.json(RECENT_EVENTS)),
  http.get('*/api/stats', () => HttpResponse.json(STATS)),
  http.get('*/api/search', () => HttpResponse.json([])),
  http.get('*/api/media/*', () => new HttpResponse(new Uint8Array([0xff, 0xd8, 0xff]), { headers: { 'content-type': 'image/jpeg' } })),
  http.post('*/agent/chat/stream', () => sseResponse([{ event: 'done', data: { reply: 'ok', citations: [], tool_calls: [] } }])),
  http.post('*/agent/chat', () => HttpResponse.json({ reply: 'ok', citations: [], tool_calls: [] })),
];

export interface SSEFrame {
  event: string;
  data: unknown;
}

export function encodeFrame(f: SSEFrame): string {
  return `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`;
}

/** A complete (non-incremental) SSE response. */
export function sseResponse(frames: SSEFrame[]): Response {
  const enc = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(enc.encode(encodeFrame(f)));
      controller.close();
    },
  });
  return new HttpResponse(stream, { headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' } });
}

/** An SSE response whose frames the test pushes manually (to observe streaming). */
export function controllableSSE() {
  const enc = new TextEncoder();
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
      // msw resolves fetch() only once the first chunk exists; an SSE comment flushes headers immediately
      // (clients must ignore comment lines per the SSE spec).
      c.enqueue(enc.encode(': stream open\n\n'));
    },
  });
  return {
    response: () =>
      new HttpResponse(stream, { headers: { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' } }),
    push: (f: SSEFrame) => ctrl.enqueue(enc.encode(encodeFrame(f))),
    pushRaw: (s: string) => ctrl.enqueue(enc.encode(s)),
    close: () => ctrl.close(),
  };
}

export const server = setupServer(...defaultHandlers);
// Keep the logger in front of per-test overrides so overridden requests are logged too.
const originalUse = server.use.bind(server);
server.use = (...handlers: Parameters<typeof server.use>) => originalUse(logger, ...handlers);
