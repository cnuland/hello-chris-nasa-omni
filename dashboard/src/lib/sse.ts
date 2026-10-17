import type { ChatStreamEvent } from '../api/types';
import { validateChatResponse } from './agent';

/**
 * Incremental SSE parser: feed text chunks, get complete events back (handles events split across chunks).
 * Understands `event: token|done|error` frames with JSON `data:`; comment lines (": ...") and unknown events are
 * ignored. Malformed JSON, and `done` payloads that fail validation (lib/agent), are reported as `error` events.
 */
export class SSEParser {
  private buffer = '';
  /** A trailing CR is held back: it may be the first half of a CRLF split across chunks. */
  private pendingCR = false;

  push(chunk: string): ChatStreamEvent[] {
    let text = (this.pendingCR ? '\r' : '') + chunk;
    this.pendingCR = text.endsWith('\r');
    if (this.pendingCR) text = text.slice(0, -1);
    this.buffer += text.replace(/\r\n?/g, '\n');
    const out: ChatStreamEvent[] = [];
    let idx: number;
    while ((idx = this.buffer.indexOf('\n\n')) >= 0) {
      const frame = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      const ev = parseFrame(frame);
      if (ev) out.push(ev);
    }
    return out;
  }
}

function parseFrame(frame: string): ChatStreamEvent | null {
  let event = 'message';
  const data: string[] = [];
  for (const line of frame.split('\n')) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (!data.length) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(data.join('\n'));
  } catch {
    return { type: 'error', detail: `malformed ${event} event from the agent` };
  }
  const obj = (payload ?? {}) as Record<string, unknown>;
  switch (event) {
    case 'token':
      return { type: 'token', text: typeof obj.text === 'string' ? obj.text : '' };
    case 'done': {
      const response = validateChatResponse(payload);
      return typeof response === 'string' ? { type: 'error', detail: response } : { type: 'done', response };
    }
    case 'error':
      return { type: 'error', detail: typeof obj.detail === 'string' ? obj.detail : 'agent error' };
    default:
      return null;
  }
}
