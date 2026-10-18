// AC-UI-8 / AC-UI-19..22: chat with the inventory agent. Streams tokens over SSE, renders a safe Markdown subset, shows
// citation chips ("label (location) · time"), a visible "looking up the inventory" status with a Stop button, keeps the
// conversation across navigation (sessionStorage) and lets the operator confirm "Proposed update" PATCHes.
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { getItem, patchItem, streamChat } from '../api/client';
import type { Citation, ItemDetail, Proposal } from '../api/types';
import { proposalPatch } from '../lib/agent';
import { formatUtcMinute, locationLabel, shortId } from '../lib/format';
import { log } from '../lib/log';
import { renderMarkdown } from '../lib/markdown';
import {
  buildHistory, clearChat, loadChat, newSessionId, saveChat, type ProposalState, type UiMessage,
} from '../state/chatSession';
import { useCachedItem } from '../state/itemCache';
import { useOperator } from '../state/operator';
import { useToasts } from '../state/toasts';

const TOOL_RE = /^[A-Za-z0-9_.-]{1,64}$/;

/** Tool names from the done payload's tool_calls (untrusted: only short identifier-like names are kept). */
function toolNames(calls: unknown[] | undefined): string[] {
  const out: string[] = [];
  for (const c of calls ?? []) {
    const name = c && typeof c === 'object' ? (c as Record<string, unknown>).name : undefined;
    if (typeof name === 'string' && TOOL_RE.test(name) && !out.includes(name)) out.push(name);
  }
  return out.slice(0, 8);
}

function show(v: unknown): string {
  if (v === null || v === undefined || v === '') return 'none';
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}

interface ProposalCardProps {
  proposal: Proposal;
  state: ProposalState;
  canConfirm: boolean;
  onConfirm: () => void;
}

/**
 * One proposed PATCH. The target entity is always identified (link by label, or by id if the item cannot be loaded)
 * and each field is shown as "current -> proposed", using the agent's `current` snapshot or GET /items/{id}.
 * The model's free-text `summary` is deliberately not shown: the operator approves the verified diff, not prose that
 * could disagree with the fields actually PATCHed.
 */
function ProposalCard({ proposal, state, canConfirm, onConfirm }: ProposalCardProps) {
  const [item, setItem] = useState<ItemDetail | null>(null);
  useEffect(() => {
    let live = true;
    getItem(proposal.entity_id)
      .then((it) => live && setItem(it))
      .catch((e) => log.debug('proposal target not loaded', { entity_id: proposal.entity_id, error: String(e) }));
    return () => {
      live = false;
    };
  }, [proposal.entity_id]);

  const current = { ...(proposal.current ?? {}), ...((item ?? {}) as Record<string, unknown>) };
  const label = typeof current.label === 'string' && current.label ? current.label : null;
  const fields = Object.entries(proposal.fields as Record<string, unknown>);
  return (
    <div className="proposal" data-testid={`proposal-${proposal.proposal_id ?? proposal.entity_id}`}>
      <strong>Proposed update</strong>
      <p>
        {'Item: '}
        <Link to={`/items/${proposal.entity_id}`}>{label ?? `Item ${proposal.entity_id.slice(0, 8)}`}</Link>
        {label && <span className="muted">{` (${proposal.entity_id.slice(0, 8)})`}</span>}
      </p>
      <ul className="proposal-fields" aria-label="Proposed changes">
        {fields.map(([k, v]) => (
          <li key={k}>{k in current ? `${k}: ${show(current[k])} → ${show(v)}` : `${k}: → ${show(v)}`}</li>
        ))}
      </ul>
      {state === 'applied' ? (
        <span className="ok">Update applied</span>
      ) : (
        <button type="button" className="primary" disabled={state === 'pending' || !canConfirm} onClick={onConfirm}>
          Confirm update
        </button>
      )}
    </div>
  );
}

/** Entity citation chip: "label (location) · time", label/location looked up via the cached GET /items/{id}. */
function EntityCitation({ id, at }: { id: string; at?: string }) {
  const item = useCachedItem(id);
  const label = item?.label || `Item ${shortId(id)}`;
  const loc = item ? locationLabel(item.last_location, item.last_location_name) : '';
  const time = formatUtcMinute(at);
  return (
    <Link to={`/items/${id}`} title={id}>
      {`${label}${loc ? ` (${loc})` : ''}${time ? ` · ${time}` : ''}`}
    </Link>
  );
}

/** A cited entity id mentioned in the reply text: rendered as a link named by the item label (or its short id). */
function IdLink({ id }: { id: string }) {
  const item = useCachedItem(id);
  return <Link to={`/items/${id}`} title={id} className="id-link">{item?.label ? `${item.label} (${shortId(id)})` : shortId(id)}</Link>;
}

function Citations({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return (
    <ul className="citations" aria-label="Citations">
      {citations.map((c, i) => (
        <li key={i}>
          {c.entity_id ? (
            <EntityCitation id={c.entity_id} at={c.at} />
          ) : c.catalog_id ? (
            <span className="muted" title="Catalog reference (usual stowage / expected count), not a sighting">
              {`Catalog ${c.catalog_id}`}
            </span>
          ) : (
            <span className="muted">{`Video window ${shortId(c.window_id)}${c.at ? ` · ${formatUtcMinute(c.at)}` : ''}`}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function AssistantText({ m }: { m: UiMessage }) {
  const cited = new Set((m.citations ?? []).map((c) => c.entity_id).filter((x): x is string => !!x));
  const body: ReactNode[] = renderMarkdown(m.content, {
    renderId: (id, key) => (cited.has(id.toLowerCase()) || cited.has(id) ? <IdLink key={key} id={id} /> : null),
  });
  return <div className="msg-text msg-md">{body}</div>;
}

function WaitingStatus({ m, elapsed }: { m: UiMessage; elapsed: number }) {
  const tools = m.tools ?? [];
  return (
    <div className="chat-status" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{m.statusText || 'Thinking: looking up the inventory…'}</span>
      {tools.length > 0 && <span className="muted">{`Tools: ${tools.join(', ')}`}</span>}
      {/* The ticking counter is hidden from assistive tech so the polite live region is not re-announced every second. */}
      <span className="muted" aria-hidden="true">{`${elapsed} s`}</span>
    </div>
  );
}

export default function ChatPage() {
  const toasts = useToasts();
  const [operator, setOperator] = useOperator();
  const op = operator.trim();
  const [initial] = useState(loadChat);
  const [sessionId, setSessionId] = useState(initial.sessionId);
  const [messages, setMessages] = useState<UiMessage[]>(initial.messages);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const nextId = useRef(initial.messages.reduce((a, m) => Math.max(a, m.id), 0) + 1);
  const inflight = useRef(new Set<string>());
  const abortRef = useRef<AbortController | null>(null);
  const stopRef = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    saveChat({ sessionId, messages });
  }, [sessionId, messages]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages]);
  useEffect(() => {
    if (!streaming) inputRef.current?.focus();
  }, [streaming]);
  useEffect(() => {
    if (!streaming) return undefined;
    const started = Date.now();
    setElapsed(0);
    const t = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(t);
  }, [streaming]);

  const updateMsg = (id: number, fn: (m: UiMessage) => UiMessage) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));

  const send = async (raw: string, base: UiMessage[] = messages) => {
    const text = raw.trim();
    if (!text || streaming) return;
    const history = buildHistory(base, text);
    const userId = nextId.current++;
    const replyId = nextId.current++;
    setMessages([...base, { id: userId, role: 'user', content: text }, { id: replyId, role: 'assistant', content: '', streaming: true }]);
    setDraft('');
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    stopRef.current = false;
    let failure: string | null = null;
    try {
      await streamChat(
        history,
        (ev) => {
          if (ev.type === 'token') updateMsg(replyId, (m) => ({ ...m, content: m.content + ev.text }));
          else if (ev.type === 'status') {
            updateMsg(replyId, (m) => ({
              ...m,
              statusText: ev.text ?? m.statusText,
              tools: ev.tool && !(m.tools ?? []).includes(ev.tool) ? [...(m.tools ?? []), ev.tool].slice(-8) : m.tools,
            }));
          } else if (ev.type === 'done') {
            const r = ev.response;
            const used = toolNames(r.tool_calls);
            updateMsg(replyId, (m) => ({
              ...m,
              content: typeof r.reply === 'string' && r.reply ? r.reply : m.content,
              streaming: false,
              citations: r.citations ?? [],
              proposals: (r.proposals ?? []).map((proposal) => ({ proposal, state: 'idle' as const })),
              tools: used.length ? used : m.tools,
            }));
          } else if (ev.type === 'error') {
            failure = ev.detail;
            toasts.push('error', `Agent error: ${ev.detail}`);
          }
        },
        ctrl.signal,
        sessionId,
      );
    } catch (e) {
      if (ctrl.signal.aborted && !stopRef.current) return; // unmounted: the restored session marks it interrupted
      if (!ctrl.signal.aborted) {
        failure = e instanceof Error ? e.message : String(e);
        log.warn('chat stream failed', { error: String(e) });
        toasts.error('Chat request failed', e);
      }
    }
    const stopped = stopRef.current;
    // Finalise: keep streamed text; report failures inline; a turn without an answer offers Retry.
    setMessages((ms) =>
      ms.flatMap((m) => {
        if (m.id === userId) {
          const reply = ms.find((x) => x.id === replyId);
          const unanswered = !!failure || (stopped && !reply?.content) ||
            (!!reply && !reply.content && !reply.citations?.length && !reply.proposals?.length);
          return [unanswered ? { ...m, failed: true } : m];
        }
        if (m.id !== replyId) return [m];
        if (failure) {
          const bubbles: UiMessage[] = [];
          if (m.content) bubbles.push({ ...m, streaming: false, stopped: true });
          bubbles.push({ id: nextId.current++, role: 'assistant', content: '', error: `The agent could not answer: ${failure}` });
          return bubbles;
        }
        if (stopped) return m.content ? [{ ...m, streaming: false, stopped: true }] : [{ ...m, streaming: false, stopped: true, content: '' }];
        // The stream ended without a reply (no done/error): drop the empty bubble; the question offers Retry.
        return m.content || m.citations?.length || m.proposals?.length ? [{ ...m, streaming: false }] : [];
      }),
    );
    abortRef.current = null;
    setStreaming(false);
  };

  const stop = () => {
    stopRef.current = true;
    abortRef.current?.abort();
  };

  const retry = (userMsg: UiMessage) => {
    const idx = messages.findIndex((m) => m.id === userMsg.id);
    if (idx < 0) return;
    // Drop the failed turn and everything after it, then ask again.
    void send(userMsg.content, messages.slice(0, idx));
  };

  const newChat = () => {
    stop();
    clearChat();
    setMessages([]);
    setSessionId(newSessionId());
    setDraft('');
    inflight.current.clear();
    inputRef.current?.focus();
  };

  const confirm = async (msgId: number, idx: number, proposal: Proposal) => {
    const key = `${msgId}:${idx}`;
    if (!op || inflight.current.has(key)) return;
    inflight.current.add(key);
    const setState = (state: ProposalState) =>
      updateMsg(msgId, (m) => ({
        ...m,
        proposals: m.proposals?.map((p, i) => (i === idx ? { ...p, state } : p)),
      }));
    setState('pending');
    try {
      await patchItem(proposal.entity_id, proposalPatch(proposal, op));
      setState('applied');
    } catch (e) {
      setState('idle');
      inflight.current.delete(key);
      toasts.error('Could not apply the proposed update', e);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void send(draft);
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send(draft);
    }
  };

  return (
    <div className="page chat">
      <div className="page-head">
        <div className="chat-title">
          <h1>Ask the inventory</h1>
          <button type="button" onClick={newChat} disabled={messages.length === 0 && !streaming}>New chat</button>
        </div>
        <div className="field operator">
          <label htmlFor="chat-operator">Operator</label>
          <input id="chat-operator" value={operator} autoComplete="name" aria-describedby="chat-operator-msg"
            onChange={(e) => setOperator(e.target.value)} />
          {!op && <p id="chat-operator-msg" className="field-error">Operator is required to confirm proposed updates.</p>}
        </div>
      </div>
      <div className="chat-log" role="log" aria-label="Conversation" aria-live="polite">
        {messages.length === 0 && (
          <p className="muted">Ask where something is, who moved it, or what changed in a module.</p>
        )}
        {messages.map((m) => (
          <div key={m.id} className={`msg msg-${m.role}${m.error ? ' msg-error' : ''}`}>
            <div className="msg-role">{m.role === 'user' ? 'You' : 'Agent'}</div>
            {m.role === 'user' ? (
              <div className="msg-text">{m.content}</div>
            ) : m.error ? (
              <div className="msg-text msg-error-text">{m.error}</div>
            ) : m.content ? (
              <AssistantText m={m} />
            ) : m.streaming ? (
              <WaitingStatus m={m} elapsed={elapsed} />
            ) : null}
            {m.stopped && <div className="muted small">{m.content ? 'Stopped: the answer may be incomplete.' : 'Stopped.'}</div>}
            {m.role === 'user' && m.failed && (
              <div className="msg-failed">
                <span className="field-error">No answer to this question.</span>
                <button type="button" disabled={streaming} onClick={() => retry(m)}>Retry</button>
              </div>
            )}
            {m.role === 'assistant' && !m.streaming && !m.error && (m.tools?.length ?? 0) > 0 && (
              <div className="muted small">{`Looked up with: ${m.tools!.join(', ')}`}</div>
            )}
            {m.citations && <Citations citations={m.citations} />}
            {m.proposals?.map(({ proposal, state }, i) => (
              <ProposalCard key={i} proposal={proposal} state={state} canConfirm={!!op}
                onConfirm={() => confirm(m.id, i, proposal)} />
            ))}
          </div>
        ))}
        <div ref={endRef} />
      </div>
      <form className="chat-input" onSubmit={onSubmit}>
        <label htmlFor="chat-msg" className="sr-only">Message</label>
        <textarea id="chat-msg" ref={inputRef} rows={2} value={draft} aria-busy={streaming}
          placeholder="Where is the pistol-grip drill?" onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown} />
        {streaming && <button type="button" className="danger" onClick={stop}>Stop</button>}
        <button type="submit" className="primary" disabled={streaming || !draft.trim()}>Send</button>
      </form>
    </div>
  );
}
