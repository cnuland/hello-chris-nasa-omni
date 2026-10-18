// AC-UI-22: the chat conversation survives navigation (e.g. opening a citation and pressing Back) for the lifetime of
// the browser tab, via sessionStorage. "New chat" clears it and starts a new agent session_id.
import type { ChatMessage, Citation, Proposal } from '../api/types';

export const CHAT_KEY = 'nasa-omni.chat.v1';
const MAX_STORED = 100;

export type ProposalState = 'idle' | 'pending' | 'applied';

export interface UiMessage {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  streaming?: boolean;
  citations?: Citation[];
  proposals?: { proposal: Proposal; state: ProposalState }[];
  /** user turn that got no answer (error, stop, interrupted): offers Retry. */
  failed?: boolean;
  /** assistant bubble that reports a failure inline (never sent back to the agent). */
  error?: string;
  /** the operator pressed Stop; partial text is kept but not replayed to the agent. */
  stopped?: boolean;
  /** tools the agent reported (status events while waiting, tool_calls when done). */
  tools?: string[];
  statusText?: string;
}

export interface ChatSession {
  sessionId: string;
  messages: UiMessage[];
}

export function newSessionId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  } catch {
    /* fall through */
  }
  return `s-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);

/**
 * Restores the stored conversation. A reply that was still streaming when the page was left is dropped and its
 * question marked failed (Retry), because the request was aborted on unmount. Malformed storage starts fresh.
 */
export function loadChat(): ChatSession {
  try {
    const raw = window.sessionStorage.getItem(CHAT_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (isObj(parsed) && typeof parsed.sessionId === 'string' && Array.isArray(parsed.messages)) {
        const msgs = (parsed.messages as unknown[]).filter(
          (m): m is UiMessage =>
            isObj(m) && typeof m.id === 'number' && (m.role === 'user' || m.role === 'assistant') && typeof m.content === 'string',
        );
        const out: UiMessage[] = [];
        for (const m of msgs) {
          if (m.streaming) {
            const prev = out[out.length - 1];
            if (prev?.role === 'user') out[out.length - 1] = { ...prev, failed: true };
            if (m.content) out.push({ ...m, streaming: false, stopped: true });
            continue;
          }
          out.push({
            ...m,
            proposals: m.proposals?.map((p) => ({ ...p, state: p.state === 'applied' ? 'applied' : 'idle' })),
          });
        }
        return { sessionId: parsed.sessionId.slice(0, 128), messages: out };
      }
    }
  } catch {
    /* unreadable: start fresh */
  }
  return { sessionId: newSessionId(), messages: [] };
}

export function saveChat(s: ChatSession): void {
  try {
    window.sessionStorage.setItem(CHAT_KEY, JSON.stringify({ sessionId: s.sessionId, messages: s.messages.slice(-MAX_STORED) }));
  } catch {
    /* storage full or unavailable: keep in memory only */
  }
}

export function clearChat(): void {
  try {
    window.sessionStorage.removeItem(CHAT_KEY);
  } catch {
    /* ignore */
  }
}

// The agent rejects > 200 messages, > 8000 chars per message and > 100 000 chars per request; stay well inside.
export const MAX_HISTORY_MESSAGES = 40;
export const MAX_HISTORY_CHARS = 60_000;
const MAX_MESSAGE_CHARS = 8000;

/** Conversation sent to the agent: completed turns only (no error bubbles, stopped or failed turns), newest kept. */
export function buildHistory(prev: UiMessage[], text: string): ChatMessage[] {
  const out: ChatMessage[] = prev
    .filter((m) => !m.streaming && !m.error && !m.failed && !m.stopped && m.content)
    .map((m) => ({ role: m.role, content: m.content.slice(0, MAX_MESSAGE_CHARS) }));
  out.push({ role: 'user', content: text.slice(0, MAX_MESSAGE_CHARS) });
  let chars = out.reduce((a, m) => a + m.content.length, 0);
  while (out.length > 1 && (out.length > MAX_HISTORY_MESSAGES || chars > MAX_HISTORY_CHARS || out[0].role === 'assistant')) {
    chars -= out.shift()!.content.length;
  }
  return out;
}
