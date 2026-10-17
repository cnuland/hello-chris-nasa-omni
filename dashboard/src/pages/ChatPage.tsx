// AC-UI-8: chat with the inventory agent. Streams tokens over SSE, shows clickable entity citations and lets the
// operator confirm "Proposed update" PATCHes.
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from 'react';
import { Link } from 'react-router-dom';
import { getItem, patchItem, streamChat } from '../api/client';
import type { ChatMessage, Citation, ItemDetail, Proposal } from '../api/types';
import { proposalPatch } from '../lib/agent';
import { formatUtc } from '../lib/format';
import { log } from '../lib/log';
import { useOperator } from '../state/operator';
import { useToasts } from '../state/toasts';

type ProposalState = 'idle' | 'pending' | 'applied';

interface UiMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  citations?: Citation[];
  proposals?: { proposal: Proposal; state: ProposalState }[];
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

function Citations({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;
  return (
    <ul className="citations" aria-label="Citations">
      {citations.map((c, i) => (
        <li key={i}>
          {c.entity_id ? (
            <Link to={`/items/${c.entity_id}`}>{`Item ${c.entity_id.slice(0, 8)}${c.at ? ` · ${formatUtc(c.at)}` : ''}`}</Link>
          ) : c.catalog_id ? (
            <span className="muted" title="Catalog reference (usual stowage / expected count), not a sighting">
              {`Catalog ${c.catalog_id}`}
            </span>
          ) : (
            <span className="muted">{`Video window ${(c.window_id ?? '').slice(0, 8)}${c.at ? ` · ${formatUtc(c.at)}` : ''}`}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function ChatPage() {
  const toasts = useToasts();
  const [operator, setOperator] = useOperator();
  const op = operator.trim();
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const nextId = useRef(1);
  const inflight = useRef(new Set<string>());
  const abortRef = useRef<AbortController | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: 'end' });
  }, [messages]);
  useEffect(() => {
    if (!streaming) inputRef.current?.focus();
  }, [streaming]);

  const updateMsg = (id: number, fn: (m: UiMessage) => UiMessage) =>
    setMessages((ms) => ms.map((m) => (m.id === id ? fn(m) : m)));

  const send = async () => {
    const text = draft.trim();
    if (!text || streaming) return;
    const history: ChatMessage[] = [
      ...messages.filter((m) => !m.streaming && m.content).map((m) => ({ role: m.role, content: m.content })),
      { role: 'user', content: text },
    ];
    const userMsg: UiMessage = { id: nextId.current++, role: 'user', content: text };
    const replyId = nextId.current++;
    setMessages((ms) => [...ms, userMsg, { id: replyId, role: 'assistant', content: '', streaming: true }]);
    setDraft('');
    setStreaming(true);
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let failed = false;
    try {
      await streamChat(
        history,
        (ev) => {
          if (ev.type === 'token') updateMsg(replyId, (m) => ({ ...m, content: m.content + ev.text }));
          else if (ev.type === 'done') {
            const r = ev.response;
            updateMsg(replyId, (m) => ({
              ...m,
              content: typeof r.reply === 'string' && r.reply ? r.reply : m.content,
              streaming: false,
              citations: r.citations ?? [],
              proposals: (r.proposals ?? []).map((proposal) => ({ proposal, state: 'idle' as const })),
            }));
          } else if (ev.type === 'error') {
            failed = true;
            toasts.push('error', `Agent error: ${ev.detail}`);
          }
        },
        ctrl.signal,
      );
    } catch (e) {
      if (ctrl.signal.aborted) return;
      failed = true;
      log.warn('chat stream failed', { error: String(e) });
      toasts.error('Chat request failed', e);
    } finally {
      if (!ctrl.signal.aborted) {
        // Drop an empty placeholder after a failure; otherwise finalise whatever streamed.
        setMessages((ms) =>
          ms.flatMap((m) => {
            if (m.id !== replyId) return [m];
            if (!m.content && failed) return [];
            return [{ ...m, streaming: false }];
          }),
        );
        setStreaming(false);
      }
    }
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
    void send();
  };
  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="page chat">
      <div className="page-head">
        <h1>Ask the inventory</h1>
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
          <div key={m.id} className={`msg msg-${m.role}`}>
            <div className="msg-role">{m.role === 'user' ? 'You' : 'Agent'}</div>
            <div className="msg-text">{m.content || (m.streaming ? '…' : '')}</div>
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
        <button type="submit" className="primary" disabled={streaming || !draft.trim()}>Send</button>
      </form>
    </div>
  );
}
