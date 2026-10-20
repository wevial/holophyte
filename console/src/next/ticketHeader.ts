/** A local date in a header, written bare: `approved = 2026-10-19`. */
export class HeaderDate {
  constructor(readonly iso: string) {}
}
type Scalar = string | number | HeaderDate;
export type Value = Scalar | string[] | Table | Table[];
export interface Table { [key: string]: Value }

const FENCE = "+++";
const KEY_ORDER: Record<string, string[]> = {
  "": ["ui_change", "blast_radius", "estimate", "depends_on", "orchestration", "implementer"],
  "mockup": ["url", "approved", "note"],
  "mockup.boards": ["url", "caption"],
  "evidence": ["file", "state"],
};
const TABLE_ORDER = ["mockup", "mockup.boards", "evidence"];

/** Where `text`'s header sits: `start` to `end` is its TOML, between the
 *  opening `+++` line and the end of the closing one at `close`.
 *  A header opens only before the H1 and must close. */
export function headerSpan(text: string): { start: number; end: number; close: number } | null {
  let start = -1;
  for (const line of text.matchAll(/[^\n]*(?:\n|$)/g)) {
    if (!line[0]) break;
    const content = line[0].replace(/\r?\n$/, "");
    if (start < 0) {
      if (/^#(?:\s|$)/.test(content)) return null;
      if (content === FENCE) start = line.index + line[0].length;
    } else if (content === FENCE) return { start, end: line.index, close: line.index + line[0].length };
  }
  return null;
}

interface Item { kind: "key" | "table" | "array"; path: string; key: string; value?: Scalar | string[]; first: number; last: number; comment?: string }

class Cursor {
  at = 0;
  constructor(readonly text: string) {}
  get rest() { return this.text.slice(this.at); }
  line() { return this.text.slice(0, this.at).split("\n").length - 1; }
  take(pattern: RegExp): RegExpExecArray | null {
    const found = new RegExp(pattern.source, "y");
    found.lastIndex = this.at;
    const match = found.exec(this.text);
    if (match) this.at += match[0].length;
    return match;
  }
  /** Spaces, and with `lines` newlines and comments too. */
  skip(lines = false) {
    this.take(lines ? /(?:[ \t\r\n]|#[^\n]*)*/ : /[ \t]*/);
  }
  fail(what: string): never {
    throw new Error(`header line ${this.line() + 1}: ${what}`);
  }
}

const ESCAPES: Record<string, string> = { b: "\b", t: "\t", n: "\n", f: "\f", r: "\r", '"': '"', "\\": "\\" };

function basicString(cursor: Cursor): string {
  const match = cursor.take(/"((?:[^"\\\n]|\\.)*)"/) ?? cursor.fail("an unclosed string");
  return match[1]!.replace(/\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)/g, (escape, code: string) => {
    if (code.length > 1) return String.fromCodePoint(parseInt(code.slice(1), 16));
    return ESCAPES[code] ?? cursor.fail(`an unknown escape ${escape}`);
  });
}

function scalar(cursor: Cursor): Scalar {
  if (cursor.rest.startsWith('"""') || cursor.rest.startsWith("'''")) cursor.fail("a multi-line string");
  if (cursor.rest.startsWith('"')) return basicString(cursor);
  const literal = cursor.take(/'([^'\n]*)'/);
  if (literal) return literal[1]!;
  const date = cursor.take(/\d{4}-\d{2}-\d{2}(?![\dT:])/);
  if (date) return new HeaderDate(date[0]);
  const number = cursor.take(/[+-]?\d+(?:_\d+)*(?![\w.:-])/);
  if (number) return Number(number[0].replace(/_/g, ""));
  return cursor.fail("a value outside strings, whole numbers, dates and string arrays");
}

function value(cursor: Cursor): Scalar | string[] {
  if (!cursor.take(/\[/)) return scalar(cursor);
  const items: string[] = [];
  for (;;) {
    cursor.skip(true);
    if (cursor.take(/\]/)) return items;
    const item = scalar(cursor);
    if (typeof item !== "string") cursor.fail("an array holding other than strings");
    items.push(item);
    cursor.skip(true);
    if (cursor.take(/\]/)) return items;
    if (!cursor.take(/,/)) cursor.fail("an array item without a comma");
  }
}

/** Each statement of `source` with the lines it spans; a line it cannot
 *  read is left out, so one bad line keeps the rest readable. */
function scan(source: string): Item[] {
  const items: Item[] = [];
  const cursor = new Cursor(source);
  let path = "";
  while (cursor.at < source.length) {
    cursor.skip(true);
    if (cursor.at >= source.length) break;
    const first = cursor.line();
    try {
      const table = cursor.take(/(\[\[?)[ \t]*([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)[ \t]*(\]\]?)/);
      if (table) {
        if (table[1]!.length !== table[3]!.length) cursor.fail("a table header with unmatched brackets");
        path = table[2]!;
        items.push({ kind: table[1] === "[[" ? "array" : "table", path, key: "", first, last: first });
      } else {
        const key = cursor.take(/([A-Za-z0-9_-]+)[ \t]*=[ \t]*/) ?? cursor.fail("a line that is not a key, a table or a comment");
        items.push({ kind: "key", path, key: key[1]!, value: value(cursor), first, last: cursor.line() });
      }
      cursor.skip();
      const end = cursor.take(/(#[^\r\n]*)?\r?(?:\n|$)/) ?? cursor.fail("text after a value");
      if (end[1] && items.at(-1)!.kind === "key") items.at(-1)!.comment = end[1];
    } catch {
      cursor.take(/[^\n]*\n?/);
    }
  }
  return items;
}

function table(root: Table, path: string): Table {
  let at = root;
  for (const name of path.split(".")) {
    const next = at[name];
    if (Array.isArray(next) && next.length && typeof next[0] === "object") at = next[next.length - 1] as Table;
    else if (next && typeof next === "object" && !Array.isArray(next) && !(next instanceof HeaderDate)) at = next;
    else at = at[name] = {};
  }
  return at;
}

/** The keys, tables and table arrays of the header TOML `source`. */
export function readHeader(source: string): Table {
  const root: Table = {};
  for (const item of scan(source)) {
    if (item.kind === "table") table(root, item.path);
    else if (item.kind === "array") {
      const at = item.path.lastIndexOf(".");
      const parent = at < 0 ? root : table(root, item.path.slice(0, at));
      const name = item.path.slice(at + 1);
      const entries = Array.isArray(parent[name]) ? parent[name] as Table[] : (parent[name] = []);
      entries.push({});
    } else (item.path ? table(root, item.path) : root)[item.key] = item.value!;
  }
  return root;
}

const quoted = (text: string) => '"' + text.replace(/[\\"\u0000-\u001f\u007f]/g, char =>
  ({ "\\": "\\\\", '"': '\\"', "\b": "\\b", "\t": "\\t", "\n": "\\n", "\f": "\\f", "\r": "\\r" })[char] ??
  `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`) + '"';

function formatValue(value: Scalar | string[]): string {
  if (Array.isArray(value)) return `[${value.map(quoted).join(", ")}]`;
  if (value instanceof HeaderDate) return value.iso;
  return typeof value === "number" ? String(value) : quoted(value);
}

function split(source: string) {
  const eol = source.includes("\r\n") ? "\r\n" : "\n";
  const body = source.endsWith(eol) ? source.slice(0, -eol.length) : source;
  return { eol, rows: source ? body.split(/\r?\n/) : [] };
}
const join = (rows: string[], eol: string) => rows.length ? rows.join(eol) + eol : "";
const blank = (row: string | undefined) => row === undefined || !row.trim();
const rank = (order: string[], name: string) => order.includes(name) ? order.indexOf(name) : order.length;

/** The rows from each table header to the next, the top level first. */
function segments(items: Item[], count: number) {
  const heads = items.filter(item => item.kind !== "key");
  const starts = [0, ...heads.map(head => head.first)];
  return starts.map((start, index) => ({ head: index ? heads[index - 1]! : null, start, end: starts[index + 1] ?? count }));
}

/** Where a new table `path` goes: before the first table that sorts after it. */
function tableSlot(items: Item[], count: number, path: string): number {
  const later = items.find(item => item.kind !== "key" && rank(TABLE_ORDER, item.path) > rank(TABLE_ORDER, path));
  return later?.first ?? count;
}

function insertTable(rows: string[], at: number, lines: string[]) {
  const added = [...(at > 0 && !blank(rows[at - 1]) ? [""] : []), ...lines, ...(at < rows.length ? [""] : [])];
  rows.splice(at, 0, ...added);
}

/** `source` with `key` of table `path`, "" for the top level, set to
 *  `value`, or removed when it is undefined; a key already holding `value`
 *  keeps its lines as written, and every other line stays. */
export function withKey(source: string, path: string, key: string, value: Scalar | string[] | undefined): string {
  const { eol, rows } = split(source);
  const items = scan(source);
  const found = items.find(item => item.kind === "key" && item.path === path && item.key === key);
  if (found && value !== undefined && JSON.stringify(found.value) === JSON.stringify(value) && (found.value instanceof HeaderDate) === (value instanceof HeaderDate)) return source;
  const line = `${key} = ${value === undefined ? "" : formatValue(value)}`;
  if (found) {
    rows.splice(found.first, found.last - found.first + 1, ...(value === undefined ? [] : [found.comment ? `${line} ${found.comment}` : line]));
    return join(rows, eol);
  }
  if (value === undefined) return source;
  const segment = segments(items, rows.length).find(each => (each.head?.kind === "table" && each.head.path === path) || (!each.head && !path));
  if (!segment) {
    insertTable(rows, tableSlot(items, rows.length, path), [`[${path}]`, line]);
    return join(rows, eol);
  }
  const order = KEY_ORDER[path] ?? [];
  const keys = items.filter(item => item.kind === "key" && item.path === path && item.first >= segment.start && item.first < segment.end);
  const before = keys.filter(item => rank(order, item.key) < rank(order, key)).at(-1);
  const after = keys.find(item => rank(order, item.key) > rank(order, key));
  let at = before ? before.last + 1 : after ? after.first : segment.end;
  if (!before && !after) while (at > segment.start + (segment.head ? 1 : 0) && blank(rows[at - 1])) at -= 1;
  rows.splice(at, 0, line);
  return join(rows, eol);
}

/** `source` with its `[[path]]` entries replaced by `entries`, each entry's
 *  keys in their header order. */
export function withEntries(source: string, path: string, entries: Table[]): string {
  const { eol, rows } = split(source);
  const items = scan(source);
  const old = segments(items, rows.length).filter(each => each.head?.kind === "array" && each.head.path === path);
  let at = old[0]?.start ?? tableSlot(items, rows.length, path);
  for (const segment of old.slice().reverse()) {
    rows.splice(segment.start, segment.end - segment.start);
    if (segment.start < at) at -= segment.end - segment.start;
  }
  while (at === rows.length && at > 0 && blank(rows[at - 1])) rows.splice(--at, 1);
  const order = KEY_ORDER[path] ?? [];
  const lines = entries.flatMap((entry, index) => {
    const keys = Object.keys(entry).sort((a, b) => rank(order, a) - rank(order, b));
    return [...(index ? [""] : []), `[[${path}]]`, ...keys.map(key => `${key} = ${formatValue(entry[key] as Scalar)}`)];
  });
  if (lines.length) insertTable(rows, at, lines);
  return join(rows, eol);
}
