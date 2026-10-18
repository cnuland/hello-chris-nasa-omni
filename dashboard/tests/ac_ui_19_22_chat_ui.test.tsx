// UI review fixes on the chat:
//   AC-UI-19 safe Markdown; AC-UI-20 citation chips "label (location) · time"; AC-UI-21 progress status + Stop + inline
//   errors; AC-UI-22 persistence across navigation, New chat, Retry, bounded history + session_id.
import { render, screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { describe, expect, test } from 'vitest';
import { renderMarkdown } from '../src/lib/markdown';
import { SSEParser } from '../src/lib/sse';
import { buildHistory, CHAT_KEY, MAX_HISTORY_MESSAGES, type UiMessage } from '../src/state/chatSession';
import { ID } from './fixtures';
import { controllableSSE, requestsTo, server, sseResponse } from './server';
import { current, findToast, renderApp } from './render';

const STREAM = '/agent/chat/stream';
const doneWith = (data: unknown) => server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data }])));

async function openChat(path = '/chat') {
  const r = renderApp(path);
  const input = await screen.findByLabelText(/message/i);
  return { ...r, input };
}
async function send(user: ReturnType<typeof renderApp>['user'], text: string) {
  await user.type(screen.getByLabelText(/message/i), text);
  await user.click(screen.getByRole('button', { name: /send/i }));
}
const log = () => screen.getByRole('log');

function md(source: string) {
  return render(<MemoryRouter><div data-testid="md">{renderMarkdown(source)}</div></MemoryRouter>).getByTestId('md');
}

describe('AC-UI-19 safe Markdown rendering', () => {
  test('AC-UI-19 bold, italic, inline code, lists and headings render as elements (no literal ** or backticks)', () => {
    const el = md('# Result\nThe laptop is in **US Orbital Segment (USOS)** and *maybe* `NOD1`.\n\n1. first\n2. second\n\n- a\n- b');
    expect(el.querySelector('strong')?.textContent).toBe('Result');
    expect(el.querySelectorAll('strong')[1].textContent).toBe('US Orbital Segment (USOS)');
    expect(el.querySelector('em')?.textContent).toBe('maybe');
    expect(el.querySelector('code')?.textContent).toBe('NOD1');
    expect(el.querySelectorAll('ol > li')).toHaveLength(2);
    expect(el.querySelectorAll('ul > li')).toHaveLength(2);
    expect(el.textContent).not.toMatch(/\*\*|`|^#/);
  });

  test('AC-UI-19 raw HTML is shown as text and never parsed (XSS)', () => {
    const evil = '<img src=x onerror="alert(1)"> <script>alert(2)</script> <a href="javascript:alert(3)">x</a>';
    const el = md(evil);
    expect(el.querySelector('img, script, a, iframe')).toBeNull();
    expect(el.textContent).toContain('<script>alert(2)</script>');
  });

  test('AC-UI-19 Markdown links keep their text but never become anchors (javascript: or otherwise)', () => {
    const el = md('See [click me](javascript:alert(1)) and [docs](https://evil.example/)');
    expect(el.querySelector('a')).toBeNull();
    expect(el.textContent).toBe('See click me and docs');
  });

  test('AC-UI-19 no source file uses a raw-HTML injection API (dangerouslySetInnerHTML / innerHTML)', () => {
    const walk = (d: string): string[] =>
      readdirSync(d).flatMap((f) => (statSync(join(d, f)).isDirectory() ? walk(join(d, f)) : [join(d, f)]));
    const offenders = walk(resolve(__dirname, '../src'))
      .filter((f) => /\.(tsx?|jsx?)$/.test(f))
      .filter((f) => /dangerouslySetInnerHTML|\.innerHTML\s*=|insertAdjacentHTML/.test(readFileSync(f, 'utf8')));
    expect(offenders).toEqual([]);
  });

  test('AC-UI-19 fenced code blocks are literal', () => {
    const el = md('```\n**not bold** <b>x</b>\n```');
    expect(el.querySelector('pre code')?.textContent).toBe('**not bold** <b>x</b>');
    expect(el.querySelector('strong, b')).toBeNull();
  });

  test('AC-UI-19 the agent reply renders Markdown in the conversation; cited UUIDs become item links', async () => {
    doneWith({
      reply: `1. **Pistol drill** - last seen (entity_id: \`${ID.drill}\`)\n2. Unknown ${ID.camera}`,
      citations: [{ entity_id: ID.drill, at: '2026-10-17T12:00:00Z' }],
    });
    const { user } = await openChat();
    await send(user, 'where is the drill?');
    const strong = await within(log()).findByText('Pistol drill');
    expect(strong.tagName).toBe('STRONG');
    const idLink = await within(log()).findByRole('link', { name: /pistol-grip drill \(11111111\)/i });
    expect(idLink).toHaveAttribute('href', `/items/${ID.drill}`);
    // an uncited UUID stays plain text (no link to arbitrary ids)
    expect(within(log()).queryByRole('link', { name: new RegExp(ID.camera.slice(0, 8)) })).toBeNull();
    expect(log().textContent).not.toMatch(/\*\*|`/);
  });

  test('AC-UI-19 model text with HTML is inert in the page', async () => {
    doneWith({ reply: 'Hi <img src=x onerror=alert(1)><script>window.__pwned=1</script>', citations: [] });
    const { user } = await openChat();
    await send(user, 'hi');
    await within(log()).findByText(/Hi <img src=x/);
    expect(log().querySelector('script, img[src="x"]')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });
});

describe('AC-UI-20 citation chips', () => {
  test('AC-UI-20 entity citation reads "label (location) · time" and links to the item', async () => {
    doneWith({ reply: 'The drill is in LAB1S2.', citations: [{ entity_id: ID.drill, at: '2026-10-17T12:00:00Z' }] });
    const { user } = await openChat();
    await send(user, 'where is the drill?');
    const cites = await screen.findByRole('list', { name: /citations/i });
    const link = await within(cites).findByRole('link', {
      name: 'Pistol-grip drill (Lab starboard rack 2 (LAB1S2)) · 2026-10-17 12:00 UTC',
    });
    expect(link).toHaveAttribute('href', `/items/${ID.drill}`);
  });

  test('AC-UI-20 an unknown item falls back to "Item <short id>"', async () => {
    const other = '66666666-6666-4666-8666-666666666666';
    doneWith({ reply: 'x', citations: [{ entity_id: other, at: '2026-10-17T12:00:00Z' }] });
    const { user } = await openChat();
    await send(user, 'x');
    const cites = await screen.findByRole('list', { name: /citations/i });
    expect(await within(cites).findByRole('link', { name: /^Item 66666666 · 2026-10-17 12:00 UTC$/ })).toBeInTheDocument();
  });

  test('AC-UI-20 the item lookup is cached (one GET per id across chips and messages)', async () => {
    doneWith({ reply: 'x', citations: [{ entity_id: ID.drill, at: '2026-10-17T12:00:00Z' }, { entity_id: ID.drill }] });
    const { user } = await openChat();
    await send(user, 'x');
    await screen.findAllByRole('link', { name: /pistol-grip drill/i });
    await send(user, 'again');
    await waitFor(() => expect(screen.getAllByRole('list', { name: /citations/i })).toHaveLength(2));
    expect(requestsTo('GET', `/api/items/${ID.drill}`)).toHaveLength(1);
  });
});

describe('AC-UI-21 progress status, Stop and inline errors', () => {
  test('AC-UI-21 while waiting a status line says it is looking up the inventory, and names tools from status events', async () => {
    const s = controllableSSE();
    server.use(http.post(`*${STREAM}`, () => s.response()));
    const { user } = await openChat();
    await send(user, 'who used the drill?');
    const status = await within(log()).findByRole('status');
    expect(status).toHaveTextContent(/looking up the inventory/i);
    expect(status).toHaveTextContent(/\d+ s/);
    s.push({ event: 'status', data: { tool: 'list_events' } });
    s.push({ event: 'tool_call', data: { name: 'where_is' } });
    await waitFor(() => expect(status).toHaveTextContent('Tools: list_events, where_is'));
    s.push({ event: 'done', data: { reply: 'Alice used it.', citations: [], tool_calls: [{ name: 'list_events' }] } });
    s.close();
    await within(log()).findByText('Alice used it.');
    expect(within(log()).queryByRole('status')).toBeNull();
    expect(within(log()).getByText('Looked up with: list_events')).toBeInTheDocument();
  });

  test('AC-UI-21 parser maps status / tool_call events and drops invalid tool names', () => {
    const p = new SSEParser();
    expect(p.push('event: status\ndata: {"text":"Searching events","tool":"list_events"}\n\n')).toEqual([
      { type: 'status', text: 'Searching events', tool: 'list_events' },
    ]);
    expect(p.push('event: tool_call\ndata: {"name":"<script>"}\n\n')).toEqual([]);
    expect(p.push('event: tool\ndata: {"name":"where_is"}\n\n')).toEqual([{ type: 'status', tool: 'where_is' }]);
  });

  test('AC-UI-21 Stop aborts the request, keeps partial text marked stopped, and re-enables Send', async () => {
    const s = controllableSSE();
    server.use(http.post(`*${STREAM}`, () => s.response()));
    const { user } = await openChat();
    await send(user, 'where is the drill?');
    s.push({ event: 'token', data: { text: 'The drill is' } });
    await waitFor(() => expect(log()).toHaveTextContent('The drill is'));
    await user.click(screen.getByRole('button', { name: /^stop$/i }));
    await waitFor(() => expect(screen.queryByRole('button', { name: /^stop$/i })).toBeNull());
    expect(log()).toHaveTextContent(/stopped/i);
    expect(log()).toHaveTextContent('The drill is');
    await user.type(screen.getByLabelText(/message/i), 'next');
    expect(screen.getByRole('button', { name: /send/i })).toBeEnabled();
  });

  test('AC-UI-21 Stop before any text marks the question for Retry', async () => {
    const s = controllableSSE();
    server.use(http.post(`*${STREAM}`, () => s.response()));
    const { user } = await openChat();
    await send(user, 'slow question');
    await within(log()).findByRole('status');
    await user.click(screen.getByRole('button', { name: /^stop$/i }));
    expect(await within(log()).findByRole('button', { name: /retry/i })).toBeInTheDocument();
  });

  test('AC-UI-21 an in-stream error is shown inline in the conversation (and as a toast)', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'error', data: { detail: 'llm failure' } }])));
    const { user } = await openChat();
    await send(user, 'what changed in the last hour?');
    expect(await within(log()).findByText(/the agent could not answer: llm failure/i)).toBeInTheDocument();
    expect(await findToast(/llm failure/i)).toBeInTheDocument();
  });
});

describe('AC-UI-22 persistence, New chat and Retry', () => {
  test('AC-UI-22 the conversation survives navigating to a citation and back', async () => {
    doneWith({ reply: 'The drill is in LAB1S2.', citations: [{ entity_id: ID.drill, at: '2026-10-17T12:00:00Z' }] });
    const { user } = await openChat();
    await send(user, 'where is the drill?');
    const cites = await screen.findByRole('list', { name: /citations/i });
    await user.click(within(cites).getByRole('link'));
    await waitFor(() => expect(current.pathname).toBe(`/items/${ID.drill}`));
    await screen.findByRole('heading', { name: /pistol-grip drill/i });
    await user.click(screen.getByRole('link', { name: /^chat$/i }));
    expect(await within(log()).findByText('where is the drill?')).toBeInTheDocument();
    expect(within(log()).getByText('The drill is in LAB1S2.')).toBeInTheDocument();
  });

  test('AC-UI-22 requests carry a stable session_id; New chat clears the log and starts a new session', async () => {
    doneWith({ reply: 'ok.', citations: [] });
    const { user } = await openChat();
    await send(user, 'one');
    await within(log()).findByText('ok.');
    await send(user, 'two');
    await waitFor(() => expect(requestsTo('POST', STREAM)).toHaveLength(2));
    const [a, b] = requestsTo('POST', STREAM).map((r) => (r.body as { session_id?: string }).session_id);
    expect(a).toBeTruthy();
    expect(b).toBe(a);
    await waitFor(() => expect(within(log()).getAllByText('ok.')).toHaveLength(2));
    await user.click(screen.getByRole('button', { name: /new chat/i }));
    expect(within(log()).queryByText('one')).toBeNull();
    expect(window.sessionStorage.getItem(CHAT_KEY) ?? '').not.toContain('"one"');
    await send(user, 'three');
    await waitFor(() => expect(requestsTo('POST', STREAM)).toHaveLength(3));
    const third = requestsTo('POST', STREAM)[2].body as { session_id?: string; messages: unknown[] };
    expect(third.session_id).toBeTruthy();
    expect(third.session_id).not.toBe(a);
    expect(third.messages).toEqual([{ role: 'user', content: 'three' }]);
  });

  test('AC-UI-22 a failed request marks the question with Retry, which resends it and drops the failed turn', async () => {
    let calls = 0;
    server.use(
      http.post(`*${STREAM}`, () => {
        calls += 1;
        return calls === 1
          ? HttpResponse.json({ detail: 'bad gateway' }, { status: 502 })
          : sseResponse([{ event: 'done', data: { reply: 'Answered.', citations: [] } }]);
      }),
    );
    const { user } = await openChat();
    await send(user, 'where is the drill?');
    const retry = await within(log()).findByRole('button', { name: /retry/i });
    expect(within(log()).getByText(/the agent could not answer/i)).toBeInTheDocument();
    await user.click(retry);
    await within(log()).findByText('Answered.');
    expect(within(log()).queryByText(/the agent could not answer/i)).toBeNull();
    expect(within(log()).getAllByText('where is the drill?')).toHaveLength(1);
    const body = requestsTo('POST', STREAM)[1].body as { messages: unknown[] };
    expect(body.messages).toEqual([{ role: 'user', content: 'where is the drill?' }]);
  });

  test('AC-UI-22 a reply interrupted by leaving the page is restored as a failed question (Retry), not a stuck "…"', async () => {
    const stored: UiMessage[] = [
      { id: 1, role: 'user', content: 'slow one' },
      { id: 2, role: 'assistant', content: '', streaming: true },
    ];
    window.sessionStorage.setItem(CHAT_KEY, JSON.stringify({ sessionId: 's1', messages: stored }));
    await openChat();
    expect(within(log()).getByText('slow one')).toBeInTheDocument();
    expect(within(log()).getByRole('button', { name: /retry/i })).toBeInTheDocument();
    expect(within(log()).queryByRole('status')).toBeNull();
  });

  test('AC-UI-22 corrupt sessionStorage starts a fresh conversation without crashing', async () => {
    window.sessionStorage.setItem(CHAT_KEY, '{not json');
    await openChat();
    expect(screen.getByText(/ask where something is/i)).toBeInTheDocument();
  });

  test('AC-UI-22 buildHistory skips error/stopped/failed turns, caps the length and starts with a user turn', () => {
    const msgs: UiMessage[] = [
      { id: 1, role: 'user', content: 'q1', failed: true },
      { id: 2, role: 'assistant', content: '', error: 'boom' },
      { id: 3, role: 'user', content: 'q2' },
      { id: 4, role: 'assistant', content: 'partial', stopped: true },
      { id: 5, role: 'user', content: 'q3' },
      { id: 6, role: 'assistant', content: 'a3' },
    ];
    expect(buildHistory(msgs, 'q4')).toEqual([
      { role: 'user', content: 'q2' },
      { role: 'user', content: 'q3' },
      { role: 'assistant', content: 'a3' },
      { role: 'user', content: 'q4' },
    ]);
    const many: UiMessage[] = Array.from({ length: 100 }, (_, i) => ({ id: i, role: i % 2 ? 'assistant' : 'user', content: `m${i}` }));
    const h = buildHistory(many, 'last');
    expect(h.length).toBeLessThanOrEqual(MAX_HISTORY_MESSAGES);
    expect(h[0].role).toBe('user');
    expect(h.at(-1)).toEqual({ role: 'user', content: 'last' });
    const huge: UiMessage[] = [{ id: 1, role: 'user', content: 'x'.repeat(50_000) }, { id: 2, role: 'assistant', content: 'y'.repeat(50_000) }];
    expect(buildHistory(huge, 'q').reduce((a, m) => a + m.content.length, 0)).toBeLessThanOrEqual(60_000);
  });
});
