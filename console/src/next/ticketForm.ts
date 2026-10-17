import TEMPLATE from "../../../ticketTemplate.md" with { type: "text" };

export interface Fields {
  title: string;
  summary: string;
  notes: string;
  openQuestions: string;
  what: string;
  why: string;
  how: string;
  uiChange: "major" | "minor" | null;
  mockupLink: string;
  mockupApproval: string;
  mockupImages: string[];
  inScope: string[];
  outOfScope: string[];
  criteria: string[];
  verify: string[];
  estimate: number | null;
  dependsOn: string[];
}
export type Field = keyof Fields;
export interface TicketSection { name: string; heading: string; text: string }
export interface TicketForm {
  preamble: string;
  sections: TicketSection[];
  hints: Partial<Record<Field, string | string[]>>;
}
interface Line { start: number; end: number; contentEnd: number; text: string; eol: string; suffix: string; fenced: boolean; commented: boolean }

const commentPattern = /<!--[\s\S]*?-->/g;
function withoutComments(text: string): string {
  return text.replace(commentPattern, "");
}

function visibleSource(text: string) {
  let visible = "";
  const offsets: number[] = [];
  let start = 0;
  for (const match of [...text.matchAll(commentPattern), { index: text.length, 0: "" }]) {
    visible += text.slice(start, match.index);
    for (let i = start; i < match.index; i++) offsets.push(i);
    start = match.index + match[0].length;
  }
  offsets.push(text.length);
  return { visible, offsets };
}

function lines(text: string): Line[] {
  const result: Line[] = [];
  const { visible, offsets } = visibleSource(text);
  let fence = "";
  for (const match of visible.matchAll(/[^\n]*(?:\n|$)/g)) {
    if (!match[0]) continue;
    const raw = match[0];
    const content = raw.replace(/\r?\n$/, "");
    const value = content.trimEnd();
    const start = offsets[match.index];
    const contentEnd = value ? offsets[match.index + value.length - 1] + 1 : start;
    const eolStart = offsets[match.index + content.length];
    const end = raw.endsWith("\n") ? offsets[match.index + raw.length - 1] + 1 : text.length;
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(value);
    const fenced = !!fence;
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = "";
    }
    result.push({ start, end, contentEnd, text: value, suffix: text.slice(contentEnd, eolStart), eol: raw.slice(content.length), fenced, commented: !value.trim() });
  }
  return result;
}

export function parseBody(text: string): TicketForm {
  const headings = matching(text, /^## /);
  return {
    preamble: text.slice(0, headings[0]?.start ?? text.length),
    sections: headings.map((line, i) => ({
      name: line.text.slice(3).trim(),
      heading: text.slice(line.start, line.end),
      text: text.slice(line.end, headings[i + 1]?.start ?? text.length),
    })),
    hints: {},
  };
}

export function composeBody(form: TicketForm): string {
  return form.preamble + form.sections.map(section => section.heading + section.text).join("");
}

const sections: Record<Exclude<Field, "title">, string> = {
  summary: "Summary", notes: "Implementation notes", openQuestions: "Open questions",
  what: "What / Why / How", why: "What / Why / How", how: "What / Why / How", uiChange: "What / Why / How",
  mockupLink: "Mock-up", mockupApproval: "Mock-up", mockupImages: "Mock-up",
  inScope: "In scope", outOfScope: "Out of scope", criteria: "Acceptance criteria",
  verify: "Verify command(s)", estimate: "Estimate & dependencies", dependsOn: "Estimate & dependencies",
};
const order = parseBody(TEMPLATE).sections.map(section => section.name);
const labels = { what: "What", why: "Why", how: "How" };
const scopePattern = /^- (.*)$/;
const mockupLinkPattern = /^https?:\/\/\S+[ \t]*$/;
const mockupApprovalPattern = /^Approved \d{4}-\d{2}-\d{2}:.*$/;
const itemPatterns = {
  inScope: scopePattern, outOfScope: scopePattern, criteria: /^- \[ \] (.*)$/,
  mockupImages: /^(!\[[^\]]*\]\(.*\))$/,
};
type Span = { start: number; end: number; value: string; after?: number };

function paragraph(text: string, field: keyof typeof labels): Span | undefined {
  const rows = lines(text);
  const index = rows.findIndex(line => !line.fenced && !line.commented && line.text.startsWith(`**${labels[field]}:**`));
  if (index < 0) return;
  const first = rows[index];
  const prefix = new RegExp(`^\\*\\*${labels[field]}:\\*\\*[ \\t]*`).exec(first.text)![0];
  let last = first;
  for (const line of rows.slice(index + 1)) {
    if (!line.text.trim() || line.commented || /^(\*\*[^*\r\n]+:\*\*| {0,3}(?:>|#{1,6}(?:[ \t]|$)|[-+*][ \t]+|1[.)][ \t]+|(?:-[ \t]*){3,}$|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$|`{3,}|~{3,}))/.test(line.text)) break;
    last = line;
  }
  const source = visibleSource(text.slice(first.start, first.contentEnd));
  const start = first.start + source.offsets[prefix.length];
  return { start, end: last.contentEnd, after: last.end - last.eol.length, value: withoutComments(text.slice(start, last.contentEnd)).trimEnd() };
}

function matching(text: string, pattern: RegExp): Line[] {
  return lines(text).filter(line => !line.fenced && !line.commented && pattern.test(line.text));
}
function verifySpan(text: string): Span | undefined {
  const rows = lines(text);
  const index = rows.findIndex(line => !line.fenced && !line.commented && /^ {0,3}`{3,}[^`]*$/.test(line.text));
  if (index < 0) return;
  const opening = rows[index];
  const width = opening.text.trimStart().match(/^`+/)![0].length;
  const closing = rows.slice(index + 1).find(line => new RegExp(`^ {0,3}\x60{${width},}[ \\t]*$`).test(line.text));
  const end = closing?.start ?? text.length;
  return { start: opening.end, end, value: withoutComments(text.slice(opening.end, end)) };
}
const estimatePattern = /^Estimate: (.*?) min · Depends on: (.*)$/;

export function fieldOf<K extends Field>(form: TicketForm, field: K): Fields[K] {
  const text = field === "title" ? form.preamble : form.sections.find(section => section.name === sections[field as Exclude<Field, "title">])?.text ?? "";
  let value: Fields[Field];
  if (field === "title") value = matching(text, /^# /)[0]?.text.slice(2) ?? "";
  else if (field === "summary" || field === "notes" || field === "openQuestions") value = withoutComments(text).trim();
  else if (field === "what" || field === "why" || field === "how") value = paragraph(text, field)?.value ?? "";
  else if (field === "uiChange") value = (matching(text, /^\*\*UI change:\*\* (major|minor)[ \t]*$/)[0]?.text.match(/(major|minor)/)?.[1] as Fields["uiChange"]) ?? null;
  else if (field === "mockupLink") value = matching(text, mockupLinkPattern)[0]?.text.trim() ?? "";
  else if (field === "mockupApproval") value = matching(text, mockupApprovalPattern)[0]?.text ?? "";
  else if (field in itemPatterns) {
    const pattern = itemPatterns[field as keyof typeof itemPatterns];
    value = matching(text, pattern).map(line => pattern.exec(line.text)![1]);
  } else if (field === "verify") value = (verifySpan(text)?.value.replace(/\r?\n$/, "") ?? "").split(/\r?\n/).filter((line, _i, all) => line !== "" || all.length > 1);
  else {
    const match = matching(text, estimatePattern)[0]?.text.match(estimatePattern);
    value = field === "estimate" ? (match && /^\d+$/.test(match[1]) ? Number(match[1]) : null)
      : (!match || !match[2].trim() || match[2].trim() === "none" ? [] : match[2].split(/[,\s]+/).filter(Boolean));
  }
  return value as Fields[K];
}

function splice(text: string, start: number, end: number, value: string): string {
  const removed = text.slice(start, end);
  const comments = (removed.match(commentPattern) ?? []).join("");
  const eol = comments ? /\r?\n$/.exec(value || removed)?.[0] ?? "" : "";
  const replacement = eol && value.endsWith(eol) ? value.slice(0, -eol.length) : value;
  return text.slice(0, start) + replacement + comments + eol + text.slice(end);
}
function append(text: string, value: string, eol: string): string {
  const end = text.trimEnd().length;
  return text.slice(0, end) + (end ? eol + eol : eol) + value + (text.slice(end) || eol + eol);
}
function replaceLine(text: string, pattern: RegExp, value: string, eol: string): string {
  const line = matching(text, pattern)[0];
  if (!line) return value ? append(text, value, eol) : text;
  return splice(text, line.start, value || line.suffix ? line.contentEnd : line.end, value);
}
function replaceItems(text: string, field: keyof typeof itemPatterns, value: string[], eol: string): string {
  const rows = matching(text, itemPatterns[field]);
  const prefix = field === "criteria" ? "- [ ] " : field === "mockupImages" ? "" : "- ";
  const replacement = value.map(item => prefix + item).join(eol);
  if (!rows.length) return replacement ? eol + replacement + eol + text : text;
  for (const row of rows.slice().reverse()) {
    text = text.slice(0, row.start) + (row === rows[0] ? replacement : "") + (text.slice(row.start, row.contentEnd).match(commentPattern) ?? []).join("") + row.suffix + (row.suffix || (row === rows[0] && replacement) ? row.eol : "") + text.slice(row.end);
  }
  return text;
}

function editText(text: string, field: Exclude<Field, "title">, value: Fields[Field], eol: string): string {
  if (field === "summary" || field === "notes" || field === "openQuestions") {
    if (!text.trim()) return append(text, String(value), eol);
    const start = text.length - text.trimStart().length;
    return splice(text, start, text.trimEnd().length, String(value));
  }
  if (field === "what" || field === "why" || field === "how") {
    const span = paragraph(text, field);
    return span ? splice(text, span.start, span.end, String(value)) : append(text, `**${labels[field]}:** ${value}`, eol);
  }
  if (field === "uiChange") {
    const pattern = /^\*\*UI change:\*\*.*$/;
    const how = paragraph(text, "how");
    if (value !== null && how && !matching(text, pattern).length) {
      const after = how.after ?? how.end;
      return splice(text, after, after, `${eol}${eol}**UI change:** ${value}`);
    }
    return replaceLine(text, pattern, value === null ? "" : `**UI change:** ${value}`, eol);
  }
  if (field === "mockupLink") return replaceLine(text, mockupLinkPattern, String(value), eol);
  if (field === "mockupApproval") return replaceLine(text, mockupApprovalPattern, String(value), eol);
  if (field in itemPatterns) return replaceItems(text, field as keyof typeof itemPatterns, value as string[], eol);
  if (field === "verify") {
    const commands = (value as string[]).join(eol);
    const span = verifySpan(text);
    return span ? splice(text, span.start, span.end, commands ? commands + eol : "") : append(text, "```" + eol + (commands ? commands + eol : "") + "```", eol);
  }
  const match = matching(text, estimatePattern)[0]?.text.match(estimatePattern);
  const estimate = field === "estimate" ? value ?? "" : match?.[1] ?? "";
  const dependencies = field === "dependsOn" ? (value as string[]).join(", ") || "none" : match?.[2] ?? "none";
  return replaceLine(text, estimatePattern, `Estimate: ${estimate} min · Depends on: ${dependencies}`, eol);
}

export function withField<K extends Field>(form: TicketForm, field: K, value: Fields[K]): TicketForm {
  const present = field === "title" || form.sections.some(section => section.name === sections[field as Exclude<Field, "title">]);
  if (present && JSON.stringify(fieldOf(form, field)) === JSON.stringify(value)) return form;
  const eol = composeBody(form).includes("\r\n") ? "\r\n" : "\n";
  if (field === "title") {
    const row = matching(form.preamble, /^# /)[0];
    const preamble = row ? splice(form.preamble, row.start + 2, row.contentEnd, String(value)) : `# ${value}${eol}${eol}` + form.preamble;
    return { ...form, preamble };
  }
  const name = sections[field as Exclude<Field, "title">];
  const updated = form.sections.slice();
  let index = updated.findIndex(section => section.name === name);
  let preamble = form.preamble;
  if (index < 0) {
    index = updated.findIndex(section => order.indexOf(section.name) > order.indexOf(name));
    if (index < 0) index = updated.length;
    if (index > 0) {
      const previous = updated[index - 1];
      if (!previous.text.endsWith(eol)) updated[index - 1] = { ...previous, text: previous.text + eol };
    } else if (preamble && !preamble.endsWith(eol)) preamble += eol;
    updated.splice(index, 0, { name, heading: `## ${name}${eol}`, text: eol + eol });
  }
  updated[index] = { ...updated[index], text: editText(updated[index].text, field, value, eol) };
  return { ...form, preamble, sections: updated };
}

export function blankForm(): TicketForm {
  const optional = new Set(["Reproduce", "Mock-up", "Contract checks", "Evidence", "Story"]);
  const hints: TicketForm["hints"] = {};
  const uncommented = TEMPLATE.replace(/<!--[\s\S]*?-->/g, "");
  const hintTemplate = parseBody(uncommented);
  for (const field of Object.keys(sections) as Exclude<Field, "title">[]) {
    const section = hintTemplate.sections.find(section => section.name === sections[field])!.text;
    let raw: Fields[Field] = fieldOf(hintTemplate, field);
    if (field === "mockupLink") raw = /^<https?:.*>$/m.exec(section)?.[0] ?? "";
    if (field === "mockupApproval") raw = /^Approved .*$/m.exec(section)?.[0] ?? "";
    if (field === "dependsOn") raw = estimatePattern.exec(matching(section, estimatePattern)[0]?.text ?? "")?.[2] ?? "";
    if (field === "verify") raw = verifySpan(section)?.value ?? "";
    const hint = (value: string) => [...value.matchAll(/<([^>]+)>/g)].map(match => match[1].replace(/\s+/g, " ")).join(" ");
    if (Array.isArray(raw)) hints[field] = raw.map(hint);
    else if (typeof raw === "string") hints[field] = hint(raw);
  }
  let form = parseBody(uncommented.replace(/^>.*(?:\r?\n|$)/gm, "").replace(/<[^>]*>/g, "").replace("{{TITLE}}", ""));
  form.sections = form.sections.filter(section => !optional.has(section.name));
  form = withField(form, "verify", []);
  return { ...withField(form, "openQuestions", "- None"), hints };
}
