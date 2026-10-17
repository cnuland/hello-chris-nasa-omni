// Minimal multipart/form-data encoder/decoder for tests. undici's own FormData serialisation/parsing does not work
// under vitest's jsdom realm (it hangs or fails realm assertions), so the fetch shim encodes and the msw logger decodes.

export interface FilePart {
  name: string; // filename
  type: string;
  size: number;
  bytes: Uint8Array;
}

/** What the mock server received for a multipart request. */
export class MultipartBody {
  constructor(public readonly entries: [string, string | FilePart][]) {}
  get(name: string): string | FilePart | null {
    return this.entries.find(([k]) => k === name)?.[1] ?? null;
  }
  getAll(name: string): (string | FilePart)[] {
    return this.entries.filter(([k]) => k === name).map(([, v]) => v);
  }
  has(name: string): boolean {
    return this.entries.some(([k]) => k === name);
  }
}

const enc = new TextEncoder();

export async function encodeMultipart(form: FormData): Promise<{ body: Uint8Array; contentType: string }> {
  const boundary = `----vitestBoundary${Math.random().toString(16).slice(2)}`;
  const chunks: Uint8Array[] = [];
  for (const [k, v] of form.entries()) {
    if (typeof v === 'string') {
      chunks.push(enc.encode(`--${boundary}\r\nContent-Disposition: form-data; name="${k}"\r\n\r\n${v}\r\n`));
    } else {
      const f = v as File;
      const bytes = new Uint8Array(await f.arrayBuffer());
      chunks.push(
        enc.encode(
          `--${boundary}\r\nContent-Disposition: form-data; name="${k}"; filename="${f.name}"\r\n` +
            `Content-Type: ${f.type || 'application/octet-stream'}\r\n\r\n`,
        ),
      );
      chunks.push(bytes);
      chunks.push(enc.encode('\r\n'));
    }
  }
  chunks.push(enc.encode(`--${boundary}--\r\n`));
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const body = new Uint8Array(total);
  let off = 0;
  for (const c of chunks) {
    body.set(c, off);
    off += c.length;
  }
  return { body, contentType: `multipart/form-data; boundary=${boundary}` };
}

function indexOf(hay: Uint8Array, needle: Uint8Array, from: number): number {
  outer: for (let i = from; i <= hay.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) if (hay[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

export function decodeMultipart(buf: Uint8Array, contentType: string): MultipartBody {
  const m = /boundary=("?)([^";]+)\1/.exec(contentType);
  if (!m) throw new Error('no boundary');
  const delim = enc.encode(`--${m[2]}`);
  const dec = new TextDecoder();
  const entries: [string, string | FilePart][] = [];
  let pos = indexOf(buf, delim, 0);
  while (pos >= 0) {
    const start = pos + delim.length;
    if (buf[start] === 45 && buf[start + 1] === 45) break; // "--" closing
    const headerEnd = indexOf(buf, enc.encode('\r\n\r\n'), start);
    const headers = dec.decode(buf.slice(start + 2, headerEnd));
    const next = indexOf(buf, delim, headerEnd + 4);
    const content = buf.slice(headerEnd + 4, next - 2); // strip trailing CRLF
    const name = /name="([^"]*)"/.exec(headers)?.[1] ?? '';
    const filename = /filename="([^"]*)"/.exec(headers)?.[1];
    if (filename !== undefined) {
      const type = /content-type:\s*([^\r\n]+)/i.exec(headers)?.[1] ?? '';
      entries.push([name, { name: filename, type, size: content.length, bytes: content }]);
    } else {
      entries.push([name, dec.decode(content)]);
    }
    pos = next;
  }
  return new MultipartBody(entries);
}
