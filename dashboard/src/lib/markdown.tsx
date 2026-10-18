// AC-UI-19: a small, safe Markdown subset for agent replies. The text is tokenised into React elements only (no raw
// HTML injection API is used), raw HTML is shown as literal text, and Markdown links are never rendered as <a>: only
// cited entity UUIDs become in-app item links (via renderId). Supported: paragraphs, line breaks, # headings (rendered bold), - / * / 1. lists,
// ``` fenced code, `inline code`, **bold** / __bold__, *italic*, and [text](url) (text kept, url dropped).
import { Fragment, type ReactNode } from 'react';
import { UUID_RE } from './agent';

export interface MarkdownOptions {
  /** Renders a UUID found in the text; return null to keep it as plain text. */
  renderId?: (id: string, key: string) => ReactNode | null;
}

const UUID_IN_TEXT = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;
const MAX_CHARS = 20_000;

/** Plain-text pass: UUIDs may become links (via renderId), everything else is a text node. */
function textWithIds(text: string, key: string, opts: MarkdownOptions): ReactNode[] {
  if (!opts.renderId) return [text];
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(UUID_IN_TEXT)) {
    const id = m[0];
    const idx = m.index ?? 0;
    const node = UUID_RE.test(id) ? opts.renderId(id, `${key}-id${i++}`) : null;
    if (node === null) continue;
    if (idx > last) out.push(text.slice(last, idx));
    out.push(node);
    last = idx + id.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Order matters: code first (its content is literal), then links, bold, italic.
const INLINE = /(`[^`\n]+`)|(\[[^\]\n]{1,200}\]\((?:[^()\s]|\([^()\s]{0,200}\)){1,500}\))|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(\*[^*\s][^*\n]*\*)/;

export function renderInline(text: string, key: string, opts: MarkdownOptions = {}, depth = 0): ReactNode[] {
  const out: ReactNode[] = [];
  let rest = text;
  let n = 0;
  while (rest) {
    const m = depth < 4 ? INLINE.exec(rest) : null;
    if (!m) {
      out.push(...textWithIds(rest, `${key}-t${n}`, opts));
      break;
    }
    if (m.index > 0) out.push(...textWithIds(rest.slice(0, m.index), `${key}-t${n}`, opts));
    const tok = m[0];
    const k = `${key}-${n}`;
    if (m[1]) {
      const inner = tok.slice(1, -1);
      const ids = UUID_RE.test(inner) && opts.renderId ? opts.renderId(inner, k) : null;
      out.push(ids ?? <code key={k}>{inner}</code>);
    } else if (m[2]) {
      const label = tok.slice(1, tok.indexOf(']('));
      out.push(<Fragment key={k}>{renderInline(label, k, opts, depth + 1)}</Fragment>);
    } else if (m[3]) {
      out.push(<strong key={k}>{renderInline(tok.slice(2, -2), k, opts, depth + 1)}</strong>);
    } else {
      out.push(<em key={k}>{renderInline(tok.slice(1, -1), k, opts, depth + 1)}</em>);
    }
    rest = rest.slice(m.index + tok.length);
    n++;
  }
  return out;
}

const LIST_ITEM = /^\s*(?:([-*+])|(\d{1,3})[.)])\s+(.*)$/;
const HEADING = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;

/** Renders Markdown-ish agent text as safe React elements. */
export function renderMarkdown(source: string, opts: MarkdownOptions = {}): ReactNode[] {
  const lines = source.slice(0, MAX_CHARS).replace(/\r\n?/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let b = 0;

  const flushPara = () => {
    if (!para.length) return;
    const k = `p${b++}`;
    blocks.push(
      <p key={k}>
        {para.map((l, i) => (
          <Fragment key={i}>
            {i > 0 && <br />}
            {renderInline(l, `${k}-${i}`, opts)}
          </Fragment>
        ))}
      </p>,
    );
    para = [];
  };
  const flushList = () => {
    if (!list) return;
    const k = `l${b++}`;
    const items = list.items.map((it, i) => <li key={i}>{renderInline(it, `${k}-${i}`, opts)}</li>);
    blocks.push(list.ordered ? <ol key={k}>{items}</ol> : <ul key={k}>{items}</ul>);
    list = null;
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      flushPara();
      flushList();
      const code: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      blocks.push(<pre key={`c${b++}`}><code>{code.join('\n')}</code></pre>);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      flushList();
      continue;
    }
    const h = HEADING.exec(line);
    if (h) {
      flushPara();
      flushList();
      const k = `h${b++}`;
      blocks.push(<p key={k} className="md-heading"><strong>{renderInline(h[1], k, opts)}</strong></p>);
      continue;
    }
    const li = LIST_ITEM.exec(line);
    if (li) {
      flushPara();
      const ordered = !li[1];
      if (list && list.ordered !== ordered) flushList();
      if (!list) list = { ordered, items: [] };
      list.items.push(li[3]);
      continue;
    }
    if (list && /^\s{2,}\S/.test(line)) {
      // continuation of the previous list item
      list.items[list.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return blocks;
}
