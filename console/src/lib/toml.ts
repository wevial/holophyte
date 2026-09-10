/** A line-level TOML edit for the settings sheet (KO-358): read one
 *  `[table] key` out of the file's text and rewrite that key's line in
 *  place, comments, order and every other line untouched. It is not a
 *  TOML parser: a key is bound only when it sits on one plain
 *  `key = value` line -- a string, an integer, a boolean, a one-line
 *  array of those -- directly under a plain `[section]` header. A key in
 *  any other shape (a multi-line array, a quoted or dotted table name, a
 *  triple-quoted string, an inline table) is found but unread, so the
 *  sheet shows it read-only and it stays editable in the raw tab. The
 *  daemon validates the whole document on `PUT /config`, so a text this
 *  cannot read is not a failure here, only an unbound field. */

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
 *  kept, so `[[hooks]]` never reads as `[hooks]`. Each dotted part is
 *  decoded, so `["loop"]` and `[ 'a' . "b" ]` name `loop` and `a.b`, the
 *  same tables their bare forms name -- but only a plain `[bare]` header
 *  is `plain`, and only its keys bind; a key under a quoted or dotted
 *  header is found (so the sheet neither draws it empty nor duplicates
 *  it) and left unread. */
function headerOf(line: string): { name: string; plain: boolean } | null {
  const match = HEADER.exec(line);
  if (!match) return null;
  const name = tableName(match[2]!);
  return { name: match[1] === "[[" ? `[[${name}]]` : name, plain: match[1] === "[" && BARE_KEY.test(match[2]!) };
}

/** A header's inner text as the dotted name it denotes: the parts split
 *  on the dots outside quotes, each trimmed and unquoted. */
function tableName(source: string): string {
  const parts: string[] = [];
  let at = 0;
  while (at <= source.length) {
    const rest = source.slice(at);
    const lead = /^\s*/.exec(rest)![0].length;
    const end = /^["']/.test(rest.slice(lead)) ? valueEnd(rest, lead) : rest.search(/\./) < 0 ? rest.length : rest.search(/\./);
    const part = rest.slice(lead, end).trim();
    const read = /^["']/.test(part) ? readValue(part) : part;
    parts.push(typeof read === "string" ? read : part);
    const dot = rest.indexOf(".", end);
    if (dot < 0) break;
    at += dot + 1;
  }
  return parts.join(".");
}

/** The text split into lines, with the indices of every line a value
 *  runs on to -- the inner lines of a multi-line array, inline table or
 *  `"""` string -- so that a `[loop]` or a `workers = 7` inside an
 *  implementer's multi-line string is never read as a header or a key. */
interface Layout {
  lines: string[];
  continued: Set<number>;
}

function layoutOf(text: string): Layout {
  const lines = text.split("\n");
  const continued = new Set<number>();
  let offset = 0;
  for (let at = 0; at < lines.length; at += 1) {
    const assignment = keyOfLine(lines[at]!);
    if (assignment != null) {
      const from = offset + assignment.valueAt;
      const extra = text.slice(from, valueEnd(text, from)).match(/\n/g)?.length ?? 0;
      for (let run = 1; run <= extra; run += 1) {
        continued.add(at + run);
        offset += lines[at + run - 1]!.length + 1;
      }
      at += extra;
    }
    offset += lines[at]!.length + 1;
  }
  return { lines, continued };
}

/** The line span `[start, end)` of `[table]`'s body: the lines after its
 *  header up to the next header or the end of the text, and whether the
 *  header is a plain `[bare]` one; null when the text has no such
 *  header. A header-shaped line inside a multi-line value is not a
 *  header. */
export function tableSpan(text: string, table: string): { start: number; end: number; plain: boolean } | null {
  const { lines, continued } = layoutOf(text);
  let start = -1;
  let plain = false;
  for (let at = 0; at < lines.length; at += 1) {
    if (continued.has(at)) continue;
    const header = headerOf(lines[at]!);
    if (header == null) continue;
    if (start >= 0) return { start, end: at, plain };
    if (header.name === table) {
      start = at + 1;
      plain = header.plain;
    }
  }
  return start >= 0 ? { start, end: lines.length, plain } : null;
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
        const point = Number.parseInt(hex, 16);
        // Outside Unicode, or a lone surrogate: not a scalar value, so unreadable
        // rather than a RangeError from `String.fromCodePoint`.
        if (point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)) return undefined;
        out += String.fromCodePoint(point);
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
 *  the key is absent. The first assignment in the table's body counts.
 *  The value is read only when the assignment is one line under a plain
 *  header; a multi-line value or a quoted/dotted header leaves it
 *  `undefined`, its source in `raw`. */
export function findKey(text: string, ref: KeyRef): KeyHit | null {
  const { lines, continued } = layoutOf(text);
  const span = tableSpan(text, ref.table);
  if (span == null) return null;
  let offset = 0;
  for (let at = 0; at < span.start; at += 1) offset += lines[at]!.length + 1;
  for (let at = span.start; at < span.end; at += 1) {
    const line = lines[at]!;
    const assignment = continued.has(at) ? null : keyOfLine(line);
    if (assignment != null && assignment.key === ref.key) {
      const from = offset + assignment.valueAt;
      const to = valueEnd(text, from);
      const raw = text.slice(from, to);
      const end = at + (raw.match(/\n/g)?.length ?? 0) + 1;
      const bound = span.plain && end === at + 1;
      return { start: at, end, raw, value: bound ? readValue(raw) : undefined };
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
 *  place with its trailing comment kept, appended to the table's keys
 *  when the table has no such key, or with the table appended when the
 *  text has no such table. Every other byte is as it was. The sheet only
 *  calls this for a bound key; a caller writing over a multi-line value
 *  gets it collapsed to the one line the sheet binds. */
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
  const span = tableSpan(text, ref.table);
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
