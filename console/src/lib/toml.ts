/** A line-level TOML edit for the settings sheet (KO-358): read one
 *  `[table] key` out of the file's text and rewrite that key's line in
 *  place, comments, order and every other line untouched. It is not a
 *  TOML parser: only the shapes the sheet binds -- a string, an integer,
 *  a boolean, an array of strings -- are read as values, and anything
 *  else stays editable in the raw tab. The daemon validates the whole
 *  document on `PUT /config`, so a text this cannot read is not a
 *  failure here, only an unbound field. */

export type TomlValue = string | number | boolean | TomlValue[];

/** One `[table] key` name. */
export interface KeyRef {
  table: string;
  key: string;
}

/** Where a key sits in the text: its line span (end exclusive), the
 *  value's source and what it reads as, `undefined` when the value has a
 *  shape this module does not read. */
export interface KeyHit {
  start: number;
  end: number;
  raw: string;
  value: TomlValue | undefined;
}

const HEADER = /^\s*(\[\[?)\s*([^\]]*?)\s*\]\]?\s*(#.*)?$/;
const BARE_KEY = /^[A-Za-z0-9_-]+$/;

/** The header line `[name]` opens, or null for a non-header line; an
 *  array-of-tables header `[[name]]` is its own name with the brackets
 *  kept, so `[[hooks]]` never reads as `[hooks]`. */
function headerOf(line: string): string | null {
  const match = HEADER.exec(line);
  if (!match) return null;
  const name = match[2]!.replace(/\s*\.\s*/g, ".");
  return match[1] === "[[" ? `[[${name}]]` : name;
}

/** The line span `[start, end)` of `[table]`'s body: the lines after its
 *  header up to the next header or the end of the text; null when the
 *  text has no such header. */
export function tableSpan(lines: string[], table: string): { start: number; end: number } | null {
  let start = -1;
  for (let at = 0; at < lines.length; at += 1) {
    const name = headerOf(lines[at]!);
    if (name == null) continue;
    if (start >= 0) return { start, end: at };
    if (name === table) start = at + 1;
  }
  return start >= 0 ? { start, end: lines.length } : null;
}

/** The key a line assigns, bare or quoted, and where its value starts;
 *  null for a line that assigns nothing. */
function keyOfLine(line: string): { key: string; valueAt: number } | null {
  const match = /^\s*("([^"]*)"|'([^']*)'|([A-Za-z0-9_-]+))\s*=\s*/.exec(line);
  if (!match) return null;
  return { key: match[2] ?? match[3] ?? match[4]!, valueAt: match[0].length };
}

/** The end of the value that starts at `text[at]`, counted across lines
 *  for arrays, inline tables and multi-line strings: the offset just past
 *  it. A `#` outside a string ends a bare value. */
function valueEnd(text: string, at: number): number {
  let depth = 0;
  let index = at;
  while (index < text.length) {
    const char = text[index]!;
    if (char === '"' || char === "'") {
      const triple = text.startsWith(char.repeat(3), index);
      const close = triple ? char.repeat(3) : char;
      let scan = index + close.length;
      for (;;) {
        if (scan >= text.length) return text.length;
        if (char === '"' && text[scan] === "\\") scan += 2;
        else if (text.startsWith(close, scan)) break;
        else scan += 1;
      }
      index = scan + close.length;
      if (depth === 0) return index;
      continue;
    }
    if (char === "[" || char === "{") depth += 1;
    else if (char === "]" || char === "}") {
      depth -= 1;
      if (depth === 0) return index + 1;
    } else if (depth === 0 && (char === "#" || char === "\n" || char === "\r")) return index;
    else if (depth > 0 && char === "#") {
      const nl = text.indexOf("\n", index);
      index = nl < 0 ? text.length : nl;
      continue;
    }
    index += 1;
  }
  return text.length;
}

const ESCAPES: Record<string, string> = { b: "\b", t: "\t", n: "\n", f: "\f", r: "\r", '"': '"', "\\": "\\" };

/** The value `raw` (a value's source) reads as, or `undefined` for a
 *  shape the sheet does not bind: a float, a date, an inline table, a
 *  multi-line string, a mixed or nested array. */
export function readValue(raw: string): TomlValue | undefined {
  const source = raw.trim();
  if (source === "true") return true;
  if (source === "false") return false;
  if (/^[+-]?\d[\d_]*$/.test(source)) return Number(source.replace(/_/g, ""));
  if (source.startsWith('"""') || source.startsWith("'''")) return undefined;
  if (source.startsWith('"') && source.endsWith('"') && source.length >= 2) {
    const body = source.slice(1, -1);
    let out = "";
    for (let at = 0; at < body.length; at += 1) {
      const char = body[at]!;
      if (char !== "\\") {
        if (char === '"') return undefined;
        out += char;
        continue;
      }
      const next = body[at + 1] ?? "";
      if (next in ESCAPES) {
        out += ESCAPES[next]!;
        at += 1;
      } else if (next === "u" || next === "U") {
        const width = next === "u" ? 4 : 8;
        const hex = body.slice(at + 2, at + 2 + width);
        if (hex.length !== width || !/^[0-9A-Fa-f]+$/.test(hex)) return undefined;
        out += String.fromCodePoint(Number.parseInt(hex, 16));
        at += 1 + width;
      } else return undefined;
    }
    return out;
  }
  if (source.startsWith("'") && source.endsWith("'") && source.length >= 2) {
    const body = source.slice(1, -1);
    return body.includes("'") ? undefined : body;
  }
  if (source.startsWith("[") && source.endsWith("]")) {
    const items: TomlValue[] = [];
    const last = source.length - 1;
    let at = 1;
    for (;;) {
      while (at < last) {
        const char = source[at]!;
        if (char === "#") {
          const nl = source.indexOf("\n", at);
          at = nl < 0 || nl > last ? last : nl;
        } else if (/[\s,]/.test(char)) at += 1;
        else break;
      }
      if (at >= last) return items;
      const end = Math.min(valueEnd(source, at), last);
      if (end <= at) return undefined;
      const item = readValue(source.slice(at, end));
      if (item === undefined || Array.isArray(item)) return undefined;
      items.push(item);
      at = end;
    }
  }
  return undefined;
}

/** A value written the way the sheet writes it: a basic string, an
 *  integer, a boolean, or an array of those on one line. */
export function formatValue(value: TomlValue): string {
  if (typeof value === "string") {
    const escaped = value.replace(/[\\"\u0000-\u001f\u007f]/g, (char) => {
      if (char === "\\") return "\\\\";
      if (char === '"') return '\\"';
      const named = Object.entries(ESCAPES).find(([, plain]) => plain === char);
      return named ? `\\${named[0]}` : `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`;
    });
    return `"${escaped}"`;
  }
  if (typeof value === "number") return String(Math.trunc(value));
  if (typeof value === "boolean") return value ? "true" : "false";
  return `[${value.map(formatValue).join(", ")}]`;
}

/** Where `[table] key` is assigned in `text`, or null when the table or
 *  the key is absent. The first assignment in the table's body counts. */
export function findKey(text: string, ref: KeyRef): KeyHit | null {
  const lines = text.split("\n");
  const span = tableSpan(lines, ref.table);
  if (span == null) return null;
  let offset = 0;
  for (let at = 0; at < span.start; at += 1) offset += lines[at]!.length + 1;
  for (let at = span.start; at < span.end; at += 1) {
    const line = lines[at]!;
    const assignment = keyOfLine(line);
    if (assignment != null && assignment.key === ref.key) {
      const from = offset + assignment.valueAt;
      const to = valueEnd(text, from);
      const raw = text.slice(from, to);
      const end = at + (raw.match(/\n/g)?.length ?? 0) + 1;
      return { start: at, end, raw, value: readValue(raw) };
    }
    offset += line.length + 1;
  }
  return null;
}

/** `[table] key`'s value in `text`, `undefined` when absent or unread. */
export function readKey(text: string, ref: KeyRef): TomlValue | undefined {
  return findKey(text, ref)?.value;
}

/** The last line of a table's body that is not blank or a comment, so a
 *  new key lands after the table's keys and before the blank line that
 *  separates it from the next header; `start - 1` for an empty body. */
function lastKeyLine(lines: string[], span: { start: number; end: number }): number {
  for (let at = span.end - 1; at >= span.start; at -= 1) {
    const line = lines[at]!.trim();
    if (line !== "" && !line.startsWith("#")) return at;
  }
  return span.start - 1;
}

/** `text` with `[table] key` set to `value`: the key's line rewritten in
 *  place with its trailing comment kept (a multi-line value collapses to
 *  one line, its inner comments with it), appended to the table's keys
 *  when the table has no such key, or with the table appended when the
 *  text has no such table. Every other byte is as it was. */
export function writeKey(text: string, ref: KeyRef, value: TomlValue): string {
  const lines = text.split("\n");
  const key = BARE_KEY.test(ref.key) ? ref.key : formatValue(ref.key);
  const assignment = `${key} = ${formatValue(value)}`;
  const hit = findKey(text, ref);
  if (hit != null) {
    const first = lines[hit.start]!;
    const indent = /^\s*/.exec(first)![0];
    const last = lines[hit.end - 1]!;
    const rawTail = hit.raw.length - hit.raw.lastIndexOf("\n") - 1;
    const tailAt = hit.end - 1 === hit.start ? keyOfLine(first)!.valueAt + hit.raw.length : rawTail;
    const tail = last.slice(tailAt);
    const kept = tail.trim() === "" ? "" : ` ${tail.trim()}`;
    lines.splice(hit.start, hit.end - hit.start, `${indent}${assignment}${kept}`);
    return lines.join("\n");
  }
  const span = tableSpan(lines, ref.table);
  if (span == null) {
    const trimmed = text.replace(/\s+$/, "");
    return `${trimmed === "" ? "" : `${trimmed}\n\n`}[${ref.table}]\n${assignment}\n`;
  }
  lines.splice(lastKeyLine(lines, span) + 1, 0, assignment);
  return lines.join("\n");
}

/** `text` without `[table] key`'s line(s); unchanged when absent. */
export function deleteKey(text: string, ref: KeyRef): string {
  const hit = findKey(text, ref);
  if (hit == null) return text;
  const lines = text.split("\n");
  lines.splice(hit.start, hit.end - hit.start);
  return lines.join("\n");
}

/** The `[table] key` a daemon's refusal names, if it names one the way
 *  the loader writes them (`[loop] workers must be ...`); null otherwise,
 *  so the sheet shows the sentence under the raw tab instead. */
export function namedKey(message: string): KeyRef | null {
  const match = /\[([A-Za-z0-9_.-]+)\]\s+([A-Za-z0-9_-]+)\b/.exec(message);
  return match ? { table: match[1]!, key: match[2]! } : null;
}
