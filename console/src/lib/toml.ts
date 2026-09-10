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
 *  kept, so `[[hooks]]` never reads as `[hooks]`. Each dotted part is
 *  decoded, so `["loop"]` and `[ 'a' . "b" ]` name `loop` and `a.b`, the
 *  same tables their bare forms name; a part this cannot decode keeps
 *  its source, which matches nothing the sheet binds. */
function headerOf(line: string): string | null {
  const match = HEADER.exec(line);
  if (!match) return null;
  const name = tableName(match[2]!);
  return match[1] === "[[" ? `[[${name}]]` : name;
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
 *  header up to the next header or the end of the text; null when the
 *  text has no such header. A header-shaped line inside a multi-line
 *  value is not a header. */
export function tableSpan(text: string, table: string): { start: number; end: number } | null {
  const { lines, continued } = layoutOf(text);
  let start = -1;
  for (let at = 0; at < lines.length; at += 1) {
    if (continued.has(at)) continue;
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
 *  the key is absent. The first assignment in the table's body counts. */
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

/** The items written on one line of a multi-line array, each with its
 *  source text, plus the indent before the first and the tail after the
 *  last (a comma, a comment). Null for a line whose items are not all
 *  readable scalars (a nested array, a comment-only line reads as no
 *  items). */
function itemsOfLine(line: string): { head: string; items: { item: TomlValue; text: string }[]; tail: string } | null {
  const head = /^\s*/.exec(line)![0];
  const items: { item: TomlValue; text: string }[] = [];
  let at = head.length;
  let tailAt = at;
  while (at < line.length) {
    const char = line[at]!;
    if (char === "#") break;
    if (/[\s,]/.test(char)) {
      at += 1;
      continue;
    }
    const end = valueEnd(line, at);
    if (end <= at) return null;
    const item = readValue(line.slice(at, end));
    if (item === undefined || Array.isArray(item)) return null;
    items.push({ item, text: line.slice(at, end) });
    at = tailAt = end;
  }
  return { head, items, tail: items.length === 0 ? "" : line.slice(tailAt) };
}

/** The lines of a multi-line array (`key = [` opening, `]` closing,
 *  each bracket line free to carry items of its own, which are edited as
 *  if on their own line and rejoined when they come through unchanged)
 *  rewritten to hold `items`, in order, with the comment
 *  lines between them and each kept item's trailing comment as they
 *  were: an item that stays keeps its line, an item edited in place
 *  keeps its line's tail, a dropped item's line goes, and new items are
 *  appended before the closing bracket. A line holding several items
 *  stays whole while its run survives in order, else it is split one
 *  item per line, the last keeping the line's tail. Null when the span
 *  is not that shape, so the caller collapses it instead. */
function editArrayLines(lines: string[], hit: KeyHit, items: TomlValue[]): string[] | null {
  const old = hit.value;
  if (hit.end - hit.start < 2 || !Array.isArray(old)) return null;
  // The bracket lines split at their brackets: `key = [` plus whatever
  // follows, and whatever precedes `]` plus the bracket and its tail.
  const first = lines[hit.start]!;
  const bracketAt = keyOfLine(first)!.valueAt + (hit.raw.length - hit.raw.trimStart().length);
  if (first[bracketAt] !== "[") return null;
  const opening = first.slice(0, bracketAt + 1);
  const afterOpen = first.slice(bracketAt + 1);
  const lastLine = lines[hit.end - 1]!;
  const closeAt = hit.raw.length - hit.raw.lastIndexOf("\n") - 2;
  if (lastLine[closeAt] !== "]") return null;
  const beforeClose = lastLine.slice(0, closeAt);
  const closing = `${beforeClose.trim() === "" ? beforeClose : /^\s*/.exec(first)![0]}${lastLine.slice(closeAt)}`;
  const inner = lines.slice(hit.start + 1, hit.end - 1);
  let indent: string | null = inner.map((line) => itemsOfLine(line)).find((parts) => parts != null && parts.items.length > 0)?.head ?? null;
  const virtualOpen = afterOpen.trim() === "" ? null : `${indent ?? "  "}${afterOpen.trimStart()}`;
  const virtualClose = beforeClose.trim() === "" ? null : `${indent ?? "  "}${beforeClose.trimStart()}`;
  if (virtualOpen != null) inner.unshift(virtualOpen);
  if (virtualClose != null) inner.push(virtualClose);
  type Row = { line: string; item?: TomlValue; head?: string; tail?: string };
  const out: string[] = [];
  let next = 0;
  const runAt = (run: TomlValue[]) =>
    items.findIndex((_, index) => index >= next && run.every((item, offset) => items[index + offset] === item));
  const emit = (row: Row) => {
    if (row.item === undefined) {
      out.push(row.line);
      return;
    }
    const keep = runAt([row.item]);
    if (keep >= 0) {
      for (let index = next; index < keep; index += 1) out.push(`${indent}${formatValue(items[index]!)},`);
      out.push(row.line);
      next = keep + 1;
    } else if (next < items.length && !old.includes(items[next]!)) {
      // An item edited in place: the new text takes over its line and tail.
      out.push(`${row.head}${formatValue(items[next]!)}${row.tail}`);
      next += 1;
    }
  };
  for (const line of inner) {
    const parts = itemsOfLine(line);
    if (parts == null || parts.items.length === 0) {
      out.push(line);
      continue;
    }
    indent ??= parts.head;
    const run = parts.items.map((part) => part.item);
    const keep = run.length > 1 ? runAt(run) : -1;
    if (keep >= 0) {
      for (let index = next; index < keep; index += 1) out.push(`${indent}${formatValue(items[index]!)},`);
      out.push(line);
      next = keep + run.length;
      continue;
    }
    parts.items.forEach((part, index) => {
      const rowHead = index === 0 ? parts.head : indent!;
      const rowTail = index === parts.items.length - 1 ? parts.tail : ",";
      emit({ line: `${rowHead}${part.text}${rowTail}`, item: part.item, head: rowHead, tail: rowTail });
    });
  }
  indent ??= "  ";
  for (let index = next; index < items.length; index += 1) out.push(`${indent}${formatValue(items[index]!)},`);
  // Every item line but the last must end its value with a comma; the
  // last keeps whatever it had, TOML allowing a trailing one.
  const withComma = (tail: string) => (tail.trimStart().startsWith(",") ? tail : `,${tail}`);
  const itemEnd = (line: string) => {
    const parts = itemsOfLine(line);
    return parts != null && parts.items.length > 0 ? line.length - parts.tail.length : -1;
  };
  let last = -1;
  out.forEach((line, index) => {
    if (itemEnd(line) >= 0) last = index;
  });
  for (let index = 0; index < last; index += 1) {
    const end = itemEnd(out[index]!);
    if (end >= 0) out[index] = `${out[index]!.slice(0, end)}${withComma(out[index]!.slice(end))}`;
  }
  // A bracket line's own items that came through unchanged rejoin it,
  // so the line keeps its bytes.
  const openRow = virtualOpen != null && out[0] === virtualOpen ? [`${opening}${afterOpen}`, ...out.slice(1)] : [opening, ...out];
  const closeRow = virtualClose != null && openRow[openRow.length - 1] === virtualClose ? [...openRow.slice(0, -1), `${beforeClose}${lastLine.slice(closeAt)}`] : [...openRow, closing];
  return closeRow;
}

/** `text` with `[table] key` set to `value`: the key's line rewritten in
 *  place with its trailing comment kept, a multi-line array edited line
 *  by line with its inner comments kept (`editArrayLines`; any other
 *  multi-line value collapses to one line), appended to the table's keys
 *  when the table has no such key, or with the table appended when the
 *  text has no such table. Every other byte is as it was. */
export function writeKey(text: string, ref: KeyRef, value: TomlValue): string {
  const lines = text.split("\n");
  const key = BARE_KEY.test(ref.key) ? ref.key : formatValue(ref.key);
  const assignment = `${key} = ${formatValue(value)}`;
  const hit = findKey(text, ref);
  if (hit != null) {
    const edited = Array.isArray(value) ? editArrayLines(lines, hit, value) : null;
    if (edited != null) {
      lines.splice(hit.start, hit.end - hit.start, ...edited);
      return lines.join("\n");
    }
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
