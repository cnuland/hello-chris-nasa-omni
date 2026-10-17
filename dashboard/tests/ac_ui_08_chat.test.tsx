// AC-UI-8 Chat (/chat): message list, input, streaming reply, clickable citations that open the item detail;
// "Proposed update" messages render a confirm button that performs the PATCH.
//
// Agent stream contract assumed by the dashboard (see docs/tdd-log/dashboard.md):
//   POST /agent/chat/stream  {messages:[{role,content}], session_id?}  ->  text/event-stream
//   event: token  data: {"text": "..."}               (zero or more)
//   event: done   data: {reply, citations:[{entity_id|window_id, at}], tool_calls, proposals?:[{entity_id, fields, summary?}]}
//   event: error  data: {"detail": "..."}
import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, test } from 'vitest';
import { ID } from './fixtures';
import { controllableSSE, requestsTo, server, sseResponse } from './server';
import { current, findToast, renderApp } from './render';
import { SSEParser } from '../src/lib/sse';

const STREAM = '/agent/chat/stream';
const OPERATOR_KEY = 'nasa-omni.operator';

async function openChat() {
  const r = renderApp('/chat');
  const input = await screen.findByLabelText(/message/i);
  return { ...r, input };
}

async function send(user: ReturnType<typeof renderApp>['user'], input: HTMLElement, text: string) {
  await user.type(input, text);
  await user.click(screen.getByRole('button', { name: /send/i }));
}

const doneDrill = {
  reply: 'The drill is in Lab starboard rack 2.',
  citations: [
    { entity_id: ID.drill, at: '2026-10-17T12:00:00Z' },
    { window_id: ID.window1, at: '2026-10-17T11:59:00Z' },
  ],
  tool_calls: [{ name: 'where_is', arguments: { name: 'drill' } }],
};

describe('AC-UI-8 chat basics', () => {
  test('AC-UI-8 page has a message log, a labelled input and a send button', async () => {
    await openChat();
    expect(screen.getByRole('log')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /send/i })).toBeInTheDocument();
  });

  test('AC-UI-8 sending posts the conversation to /agent/chat/stream and shows the user message', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneDrill }])));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    expect(within(screen.getByRole('log')).getByText('where is the drill?')).toBeInTheDocument();
    await waitFor(() => expect(requestsTo('POST', STREAM)).toHaveLength(1));
    const body = requestsTo('POST', STREAM)[0].body as { messages: { role: string; content: string }[] };
    expect(body.messages).toEqual([{ role: 'user', content: 'where is the drill?' }]);
    expect(input).toHaveValue('');
  });

  test('AC-UI-8 empty / whitespace message is not sent', async () => {
    const { user, input } = await openChat();
    await user.type(input, '   ');
    const btn = screen.getByRole('button', { name: /send/i }) as HTMLButtonElement;
    if (!btn.disabled) await user.click(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('POST', STREAM)).toHaveLength(0);
  });

  test('AC-UI-8 Enter key sends the message', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneDrill }])));
    const { user, input } = await openChat();
    await user.type(input, 'where is the drill?{Enter}');
    await waitFor(() => expect(requestsTo('POST', STREAM)).toHaveLength(1));
  });

  test('AC-UI-8 follow-up turns send the full history (user + assistant)', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneDrill }])));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    await within(screen.getByRole('log')).findByText(/The drill is in Lab starboard rack 2\./);
    await send(user, screen.getByLabelText(/message/i), 'who moved it?');
    await waitFor(() => expect(requestsTo('POST', STREAM)).toHaveLength(2));
    const body = requestsTo('POST', STREAM)[1].body as { messages: { role: string; content: string }[] };
    expect(body.messages).toEqual([
      { role: 'user', content: 'where is the drill?' },
      { role: 'assistant', content: 'The drill is in Lab starboard rack 2.' },
      { role: 'user', content: 'who moved it?' },
    ]);
  });
});

describe('AC-UI-8 streaming', () => {
  test('AC-UI-8 tokens render incrementally before the stream completes', async () => {
    const s = controllableSSE();
    server.use(http.post(`*${STREAM}`, () => s.response()));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    s.push({ event: 'token', data: { text: 'The drill ' } });
    s.push({ event: 'token', data: { text: 'is in' } });
    const log = screen.getByRole('log');
    await waitFor(() => expect(log).toHaveTextContent('The drill is in'));
    expect(log).not.toHaveTextContent('Lab starboard rack 2');
    s.push({ event: 'token', data: { text: ' Lab starboard rack 2.' } });
    s.push({ event: 'done', data: doneDrill });
    s.close();
    await waitFor(() => expect(log).toHaveTextContent('The drill is in Lab starboard rack 2.'));
    // not duplicated by the final reply
    expect(log.textContent!.split('The drill is in Lab starboard rack 2.').length - 1).toBe(1);
  });

  test('AC-UI-8 SSE frames split across network chunks are reassembled', async () => {
    const s = controllableSSE();
    server.use(http.post(`*${STREAM}`, () => s.response()));
    const { user, input } = await openChat();
    await send(user, input, 'hi');
    s.pushRaw('event: token\ndata: {"te');
    s.pushRaw('xt": "Hello there"}\n\n');
    s.push({ event: 'done', data: { reply: 'Hello there', citations: [], tool_calls: [] } });
    s.close();
    await waitFor(() => expect(screen.getByRole('log')).toHaveTextContent('Hello there'));
  });

  test('AC-UI-8 input/send are disabled while a reply is streaming and re-enabled after', async () => {
    const s = controllableSSE();
    server.use(http.post(`*${STREAM}`, () => s.response()));
    const { user, input } = await openChat();
    await send(user, input, 'hi');
    await waitFor(() => expect(screen.getByRole('button', { name: /send/i })).toBeDisabled());
    s.push({ event: 'done', data: { reply: 'Hi.', citations: [], tool_calls: [] } });
    s.close();
    await user.type(screen.getByLabelText(/message/i), 'next');
    await waitFor(() => expect(screen.getByRole('button', { name: /send/i })).toBeEnabled());
  });

  test('AC-UI-8 HTTP error from the agent -> error toast, input usable again', async () => {
    server.use(http.post(`*${STREAM}`, () => HttpResponse.json({ detail: 'agent unavailable' }, { status: 503 })));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    expect(await findToast(/agent unavailable|503|failed|error/i)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/message/i), 'retry');
    expect(screen.getByRole('button', { name: /send/i })).toBeEnabled();
  });

  test('AC-UI-8 error event inside the stream -> error toast', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'error', data: { detail: 'LLM timeout' } }])));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    expect(await findToast(/LLM timeout|failed|error/i)).toBeInTheDocument();
  });
});

describe('AC-UI-8 citations', () => {
  test('AC-UI-8 entity citations are clickable and open the item detail', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneDrill }])));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    const cites = await screen.findByRole('list', { name: /citations/i });
    const links = within(cites).getAllByRole('link');
    expect(links).toHaveLength(1); // the window citation is not an item link
    await user.click(links[0]);
    await waitFor(() => expect(current.pathname).toBe(`/items/${ID.drill}`));
    expect(await screen.findByRole('heading', { name: /pistol-grip drill/i })).toBeInTheDocument();
  });

  test('AC-UI-8 window citations are shown (with their time) but not as item links', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneDrill }])));
    const { user, input } = await openChat();
    await send(user, input, 'where is the drill?');
    const cites = await screen.findByRole('list', { name: /citations/i });
    expect(within(cites).getAllByRole('listitem')).toHaveLength(2);
    expect(within(cites).getByText(/11:59/)).toBeInTheDocument();
  });

  test('AC-UI-8 replies without citations render no citation list', async () => {
    server.use(
      http.post(`*${STREAM}`, () =>
        sseResponse([{ event: 'done', data: { reply: "I don't have a record of that", citations: [], tool_calls: [] } }]),
      ),
    );
    const { user, input } = await openChat();
    await send(user, input, 'where is the hammer?');
    await within(screen.getByRole('log')).findByText(/I don't have a record of that/);
    expect(screen.queryByRole('list', { name: /citations/i })).toBeNull();
  });
});

describe('AC-UI-8 proposed updates', () => {
  // Confirming requires a human operator (shared with the review page via localStorage); seed it for these tests.
  beforeEach(() => window.localStorage.setItem(OPERATOR_KEY, 'alice'));
  const doneProposal = {
    reply: 'I can record that the drill is now in Node 2 deck bay 4.',
    citations: [{ entity_id: ID.drill, at: '2026-10-17T12:00:00Z' }],
    tool_calls: [{ name: 'propose_update' }],
    proposals: [{ entity_id: ID.drill, fields: { last_location: 'NOD2D4' }, summary: 'Move drill to NOD2D4' }],
  };

  test('AC-UI-8 a proposal renders as "Proposed update" with a confirm button, and nothing is PATCHed yet', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneProposal }])));
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill to node 2 deck 4');
    expect(await screen.findByText(/proposed update/i)).toBeInTheDocument();
    expect(screen.getByText(/NOD2D4/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /confirm/i })).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('PATCH', `/api/items/${ID.drill}`)).toHaveLength(0);
  });

  test('AC-UI-8 confirm performs the PATCH with the proposed fields and marks it applied', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneProposal }])));
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill to node 2 deck 4');
    await user.click(await screen.findByRole('button', { name: /confirm/i }));
    await waitFor(() => expect(requestsTo('PATCH', `/api/items/${ID.drill}`)).toHaveLength(1));
    expect(requestsTo('PATCH', `/api/items/${ID.drill}`)[0].body).toMatchObject({ last_location: 'NOD2D4' });
    await waitFor(() => expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull());
    expect(screen.getByText(/applied|confirmed|updated/i)).toBeInTheDocument();
  });

  test('AC-UI-8 failed confirm shows an error toast and keeps the confirm button', async () => {
    server.use(
      http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneProposal }])),
      http.patch(`*/api/items/${ID.drill}`, () => HttpResponse.json({ detail: 'entity is merged' }, { status: 409 })),
    );
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill to node 2 deck 4');
    await user.click(await screen.findByRole('button', { name: /confirm/i }));
    expect(await findToast(/entity is merged|409|failed|error/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /confirm/i })).toBeEnabled();
  });

  test('AC-UI-8 confirm can only be applied once (no double PATCH on double click)', async () => {
    server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data: doneProposal }])));
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill to node 2 deck 4');
    const btn = await screen.findByRole('button', { name: /confirm/i });
    await user.dblClick(btn);
    await new Promise((r) => setTimeout(r, 50));
    expect(requestsTo('PATCH', `/api/items/${ID.drill}`)).toHaveLength(1);
  });
});

describe('AC-UI-8 SSE parser (lib/sse)', () => {
  test('AC-UI-8 parses token and done events', () => {
    const p = new SSEParser();
    const evs = p.push(
      'event: token\ndata: {"text":"Hi"}\n\nevent: done\ndata: {"reply":"Hi","citations":[],"tool_calls":[]}\n\n',
    );
    expect(evs).toEqual([
      { type: 'token', text: 'Hi' },
      { type: 'done', response: { reply: 'Hi', citations: [], tool_calls: [] } },
    ]);
  });

  test('AC-UI-8 buffers partial frames across pushes', () => {
    const p = new SSEParser();
    expect(p.push('event: token\nda')).toEqual([]);
    expect(p.push('ta: {"text":"A"}\n\n')).toEqual([{ type: 'token', text: 'A' }]);
  });

  test('AC-UI-8 handles CRLF line endings and ignores comments/keep-alives', () => {
    const p = new SSEParser();
    expect(p.push(': ping\r\n\r\nevent: token\r\ndata: {"text":"B"}\r\n\r\n')).toEqual([{ type: 'token', text: 'B' }]);
  });

  test('AC-UI-8 maps error events', () => {
    const p = new SSEParser();
    expect(p.push('event: error\ndata: {"detail":"boom"}\n\n')).toEqual([{ type: 'error', detail: 'boom' }]);
  });

  test('AC-UI-8 malformed JSON data does not throw (event skipped or reported as error)', () => {
    const p = new SSEParser();
    let out: ReturnType<SSEParser['push']> = [];
    expect(() => {
      out = p.push('event: token\ndata: {not json\n\n');
    }).not.toThrow();
    expect(out.every((e) => e.type === 'error')).toBe(true);
  });
});

// ---- Review round 1 ----
// A proposal exactly as services/agent/nasa_agent/harness.py forwards it: no `summary`, no `current`.
const PROPOSAL_ID = 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee';
const realProposal = {
  reply: 'I can record that the drill is now in Node 2 deck bay 4.',
  citations: [{ entity_id: ID.drill, at: '2026-10-17T12:00:00Z' }],
  tool_calls: [{ name: 'propose_update' }],
  proposals: [{ proposal_id: PROPOSAL_ID, entity_id: ID.drill, fields: { last_location: 'NOD2D4' }, status: 'pending' }],
};
const doneWith = (data: unknown) => server.use(http.post(`*${STREAM}`, () => sseResponse([{ event: 'done', data }])));
const proposalCard = () => screen.findByTestId(`proposal-${PROPOSAL_ID}`);

describe('AC-UI-8 proposal confirm is attributed to the human operator (review round 1)', () => {
  test('AC-UI-8 Confirm stays disabled until an operator is entered, then PATCH carries operator + proposal note', async () => {
    doneWith(realProposal);
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill to node 2 deck 4');
    const card = await proposalCard();
    const btn = within(card).getByRole('button', { name: /confirm/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/operator is required/i)).toBeInTheDocument();
    await user.click(btn);
    await user.type(screen.getByLabelText(/^operator$/i), 'alice');
    await waitFor(() => expect(btn).toBeEnabled());
    await user.click(btn);
    await waitFor(() => expect(requestsTo('PATCH', `/api/items/${ID.drill}`)).toHaveLength(1));
    expect(requestsTo('PATCH', `/api/items/${ID.drill}`)[0].body).toEqual({
      last_location: 'NOD2D4',
      operator: 'alice',
      note: `via agent proposal ${PROPOSAL_ID}`,
    });
    // the operator name is shared with the review page
    expect(window.localStorage.getItem(OPERATOR_KEY)).toBe('alice');
  });

  test('AC-UI-8 operator is prefilled from the shared localStorage operator', async () => {
    window.localStorage.setItem(OPERATOR_KEY, 'bob');
    doneWith(realProposal);
    const { user, input } = await openChat();
    expect(screen.getByLabelText(/^operator$/i)).toHaveValue('bob');
    await send(user, input, 'I moved the drill');
    expect(within(await proposalCard()).getByRole('button', { name: /confirm/i })).toBeEnabled();
  });

  test('AC-UI-8 model-supplied operator/note, unknown keys and invalid values are dropped from the PATCH', async () => {
    window.localStorage.setItem(OPERATOR_KEY, 'alice');
    doneWith({
      ...realProposal,
      proposals: [{
        proposal_id: PROPOSAL_ID,
        entity_id: ID.drill,
        fields: {
          last_location: 'NOD2D4', operator: 'mallory', note: 'approved by the commander', merged_into: ID.camera,
          status: 'merged', needs_review: 'yes', label: 42, attributes: ['x'], description: 'Orange drill',
        },
      }],
    });
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill');
    await user.click(within(await proposalCard()).getByRole('button', { name: /confirm/i }));
    await waitFor(() => expect(requestsTo('PATCH', `/api/items/${ID.drill}`)).toHaveLength(1));
    expect(requestsTo('PATCH', `/api/items/${ID.drill}`)[0].body).toEqual({
      last_location: 'NOD2D4',
      description: 'Orange drill',
      operator: 'alice',
      note: `via agent proposal ${PROPOSAL_ID}`,
    });
  });

  test('AC-UI-8 a proposal with no valid fields (only operator/unknown keys) offers no Confirm button', async () => {
    window.localStorage.setItem(OPERATOR_KEY, 'alice');
    doneWith({ ...realProposal, proposals: [{ entity_id: ID.drill, fields: { operator: 'mallory', foo: 1 } }] });
    const { user, input } = await openChat();
    await send(user, input, 'hi');
    await within(screen.getByRole('log')).findByText(/I can record that/);
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull();
  });
});

describe('AC-UI-8 proposal identifies the target entity (review round 1)', () => {
  test('AC-UI-8 without a summary the card links to the item by label and shows current -> proposed', async () => {
    doneWith(realProposal);
    const { user, input } = await openChat();
    await send(user, input, 'I moved the drill to node 2 deck 4');
    const card = await proposalCard();
    const link = await within(card).findByRole('link', { name: /pistol-grip drill/i });
    expect(link).toHaveAttribute('href', `/items/${ID.drill}`);
    await waitFor(() => expect(card).toHaveTextContent(/last_location:\s*LAB1S2\s*→\s*NOD2D4/));
    expect(requestsTo('GET', `/api/items/${ID.drill}`).length).toBeGreaterThanOrEqual(1);
  });

  test('AC-UI-8 if the item cannot be loaded the card still shows the entity id as a link', async () => {
    const other = '66666666-6666-4666-8666-666666666666';
    doneWith({ ...realProposal, proposals: [{ proposal_id: PROPOSAL_ID, entity_id: other, fields: { status: 'dismissed' } }] });
    const { user, input } = await openChat();
    await send(user, input, 'dismiss it');
    const card = await proposalCard();
    const link = await within(card).findByRole('link', { name: new RegExp(other.slice(0, 8)) });
    expect(link).toHaveAttribute('href', `/items/${other}`);
    expect(card).toHaveTextContent(/status/);
    expect(card).toHaveTextContent(/dismissed/);
  });
});

describe('AC-UI-8 untrusted done payloads (review round 1)', () => {
  test('AC-UI-8 citations that are not an array -> error toast, the app shell survives', async () => {
    doneWith({ reply: 'hello', citations: 'nope', tool_calls: [] });
    const { user, input } = await openChat();
    await send(user, input, 'hi');
    expect(await findToast(/agent|malformed|invalid/i)).toBeInTheDocument();
    expect(screen.getByRole('navigation')).toBeInTheDocument();
    expect(screen.getByLabelText(/message/i)).toBeInTheDocument();
  });

  test('AC-UI-8 proposals with a missing fields object or non-string entity_id are dropped without crashing', async () => {
    doneWith({
      reply: 'Here you go.',
      citations: [{ entity_id: 12345 }, { entity_id: 'not-a-uuid' }, { entity_id: ID.drill }],
      proposals: [{ entity_id: 42, fields: { label: 'x' } }, { entity_id: ID.drill }, { entity_id: ID.drill, fields: 'x' }],
    });
    const { user, input } = await openChat();
    await send(user, input, 'hi');
    await within(screen.getByRole('log')).findByText('Here you go.');
    expect(screen.getByRole('navigation')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /confirm/i })).toBeNull();
    const cites = screen.getByRole('list', { name: /citations/i });
    expect(within(cites).getAllByRole('link')).toHaveLength(1);
  });

  test('AC-UI-8 parser turns a done payload with a non-array citations field into an error event', () => {
    const p = new SSEParser();
    const evs = p.push('event: done\ndata: {"reply":"x","citations":"nope"}\n\n');
    expect(evs).toHaveLength(1);
    expect(evs[0].type).toBe('error');
  });

  test('AC-UI-8 parser turns a non-object done payload into an error event', () => {
    const p = new SSEParser();
    expect(p.push('event: done\ndata: [1,2]\n\n')[0].type).toBe('error');
    expect(p.push('event: done\ndata: {"reply":5,"citations":[]}\n\n')[0].type).toBe('error');
  });
});

describe('AC-UI-8 SSE parser CRLF split across chunks (review round 1)', () => {
  test('AC-UI-8 a CRLF split between two pushes is not a frame boundary', () => {
    const p = new SSEParser();
    const out = [...p.push('event: token\r'), ...p.push('\ndata: {"text":"Hi"}\r\n\r\n')];
    expect(out).toEqual([{ type: 'token', text: 'Hi' }]);
  });

  test('AC-UI-8 a lone trailing CR still terminates a line once the stream continues', () => {
    const p = new SSEParser();
    const out = [...p.push('event: token\rdata: {"text":"A"}\r'), ...p.push('\r: ping\n')];
    expect(out).toEqual([{ type: 'token', text: 'A' }]);
  });
});

// AC-AGT-11 (agent): catalog facts are cited as {catalog_id, at?}; entity_id/window_id citations are unchanged.
describe('AC-UI-8 catalog citations (AC-AGT-11)', () => {
  const doneCatalog = {
    reply: 'The Fluke multimeter is usually kept in LAB1; the drill was last seen in LAB1S2.',
    citations: [
      { catalog_id: 'multimeter', at: '2026-09-01T00:00:00Z' },
      { catalog_id: 'food-pack' },
      { entity_id: ID.drill, at: '2026-10-17T12:00:00Z' },
    ],
    tool_calls: [{ name: 'lookup_catalog', arguments: { q: 'multimeter' } }],
  };

  test('AC-UI-8 catalog citations are shown as catalog chips (not item links) next to entity citations', async () => {
    doneWith(doneCatalog);
    const { user, input } = await openChat();
    await send(user, input, 'where is the multimeter usually kept?');
    const cites = await screen.findByRole('list', { name: /citations/i });
    expect(within(cites).getAllByRole('listitem')).toHaveLength(3);
    expect(within(cites).getByText(/Catalog multimeter/)).toBeInTheDocument();
    expect(within(cites).getByText(/Catalog food-pack/)).toBeInTheDocument();
    const links = within(cites).getAllByRole('link');
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute('href', `/items/${ID.drill}`);
    expect(within(cites).queryByText(/Video window/)).toBeNull();
  });

  test('AC-UI-8 malformed catalog citations are dropped without crashing', async () => {
    doneWith({
      reply: 'Catalog facts.',
      citations: [
        { catalog_id: 42 },
        { catalog_id: '' },
        { catalog_id: 'NOT A SLUG <script>' },
        { catalog_id: 'x'.repeat(200) },
        { catalog_id: 'multimeter', at: 7 },
      ],
    });
    const { user, input } = await openChat();
    await send(user, input, 'hi');
    await within(screen.getByRole('log')).findByText('Catalog facts.');
    const cites = screen.getByRole('list', { name: /citations/i });
    expect(within(cites).getAllByRole('listitem')).toHaveLength(1);
    expect(within(cites).getByText('Catalog multimeter')).toBeInTheDocument();
  });

  test('AC-UI-8 parser keeps catalog citations in the done payload', () => {
    const p = new SSEParser();
    const evs = p.push(
      'event: done\ndata: {"reply":"x","citations":[{"catalog_id":"multimeter","at":"2026-09-01T00:00:00Z"}]}\n\n',
    );
    expect(evs).toEqual([
      { type: 'done', response: { reply: 'x', citations: [{ catalog_id: 'multimeter', at: '2026-09-01T00:00:00Z' }] } },
    ]);
  });
});
