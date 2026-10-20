export type TomlKind = "table" | "key" | "string" | "number" | "bool" | "date" | "comment" | "punct" | "text";
export type TomlSpan = { kind: TomlKind; text: string };
type Open = '"""' | "'''" | null;

const KEY_PART = String.raw`(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')`;
const KEY = new RegExp(String.raw`^${KEY_PART}(?:[ \t]*\.[ \t]*${KEY_PART})*[ \t]*(?==)`);
const TABLE = new RegExp(String.raw`^\[\[?[ \t]*${KEY_PART}(?:[ \t]*\.[ \t]*${KEY_PART})*[ \t]*\]\]?`);
const DATE = /^(?:\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})?)?|\d{2}:\d{2}:\d{2}(?:\.\d+)?)/;
const NUMBER = /^(?:[+-]?(?:inf|nan)|0x[0-9A-Fa-f_]+|0o[0-7_]+|0b[01_]+|[+-]?\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?[\d_]+)?)/;
const BOOL = /^(?:true|false)/;
const WORD_END = /^(?:$|[\s,\]}#])/;

function closeAt(line: string, from: number, quote: '"""' | "'''"): number {
  for (let index = from; index < line.length; index++) {
    if (quote === '"""' && line[index] === "\\") { index++; continue; }
    if (line.startsWith(quote, index)) {
      let end = index + 3;
      while (end < index + 5 && line[end] === quote[0]) end++;
      return end;
    }
  }
  return -1;
}

function quotedEnd(line: string, from: number): number {
  const quote = line[from];
  for (let index = from + 1; index < line.length; index++) {
    if (quote === '"' && line[index] === "\\") { index++; continue; }
    if (line[index] === quote) return index + 1;
  }
  return line.length;
}

function scanLine(line: string, open: Open): { spans: TomlSpan[]; open: Open } {
  const spans: TomlSpan[] = [];
  const push = (kind: TomlKind, text: string) => {
    const last = spans[spans.length - 1];
    if (last && (last.kind === kind || /^\s+$/.test(text))) last.text += text;
    else spans.push({ kind, text });
  };
  let at = 0;
  const fresh = !open;
  if (open) {
    const end = closeAt(line, 0, open);
    if (end < 0) return { spans: line ? [{ kind: "string", text: line }] : [], open };
    push("string", line.slice(0, end));
    at = end;
    open = null;
  }
  const lead = /^\s*/.exec(line.slice(at))![0];
  if (lead) push("text", lead);
  at += lead.length;
  const table = fresh ? TABLE.exec(line.slice(at)) : null;
  if (table) { push("table", table[0]); at += table[0].length; }
  const stack: string[] = [];
  let expectKey = fresh && !table;
  while (at < line.length) {
    const rest = line.slice(at);
    const space = /^\s+/.exec(rest);
    if (space) { push("text", space[0]); at += space[0].length; continue; }
    const key = expectKey ? KEY.exec(rest) : null;
    expectKey = false;
    if (key) { push("key", key[0]); push("punct", "="); at += key[0].length + 1; continue; }
    const char = rest[0]!;
    if (char === "#") { push("comment", rest); break; }
    const multi = rest.startsWith('"""') ? '"""' : rest.startsWith("'''") ? "'''" : null;
    if (multi) {
      const end = closeAt(line, at + 3, multi);
      if (end < 0) { push("string", rest); return { spans, open: multi }; }
      push("string", line.slice(at, end)); at = end; continue;
    }
    if (char === '"' || char === "'") { const end = quotedEnd(line, at); push("string", line.slice(at, end)); at = end; continue; }
    if ("[]{},=".includes(char)) {
      spans.push({ kind: "punct", text: char });
      if (char === "[" || char === "{") stack.push(char);
      if (char === "]" || char === "}") stack.pop();
      expectKey = char === "{" || (char === "," && stack[stack.length - 1] === "{");
      at++; continue;
    }
    const literal = ([["date", DATE], ["bool", BOOL], ["number", NUMBER]] as const)
      .map(([kind, pattern]) => ({ kind, match: pattern.exec(rest) }))
      .find(({ match }) => match && WORD_END.test(rest.slice(match[0].length)));
    if (literal) { push(literal.kind, literal.match![0]); at += literal.match![0].length; continue; }
    const word = /^[^\s,\]}#[{="']+/.exec(rest)?.[0] ?? char;
    push("text", word);
    at += word.length;
  }
  return { spans, open };
}

export function tomlTokenLines(text: string): TomlSpan[][] {
  let open: Open = null;
  return text.split("\n").map((line) => {
    const scanned = scanLine(line, open);
    open = scanned.open;
    return scanned.spans;
  });
}
