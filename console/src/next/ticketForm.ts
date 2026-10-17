import TEMPLATE from "../../../ticketTemplate.md" with { type: "text" };
import { same, type EditFields } from "./ticketEdit";

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
interface Line { start: number; end: number; contentEnd: number; text: string; eol: string; suffix: string; fenced: boolean }

const commentPattern = /<!--[\s\S]*?-->/g;
function withoutComments(text: string): string {
  return text.replace(commentPattern, "");
}

function visibleSource(text: string) {
  let visible = "";
  const offsets: number[] = [];
  const comments: string[] = [];
  let start = 0;
  let fence = "";
  const markers = /^ {0,3}(`{3,}|~{3,})([^\r\n]*)/gm;
  const tokens = /^ {0,3}(`{3,}|~{3,})([^\r\n]*)|<!--[\s\S]*?-->/gm;
  while (start < text.length) {
    const pattern = fence ? markers : tokens;
    pattern.lastIndex = start;
    const match = pattern.exec(text);
    const comment = match && !match[1];
    if (comment) comments.push(match[0]);
    const end = match ? match.index + (comment ? 0 : match[0].length) : text.length;
    visible += text.slice(start, end);
    for (let i = start; i < end; i++) offsets.push(i);
    if (match?.[1]) {
      if (!fence) fence = match[1];
      else if (match[1][0] === fence[0] && match[1].length >= fence.length && !match[2].trim()) fence = "";
    }
    start = match ? match.index + match[0].length : text.length;
  }
  offsets.push(text.length);
  return { visible, offsets, comments };
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
    result.push({ start, end, contentEnd, text: value, suffix: text.slice(contentEnd, eolStart), eol: raw.slice(content.length), fenced });
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
const htmlParagraphBoundary = /^ {0,3}<(?:\?|![A-Z]|!\[CDATA\[|(?:script|pre|style|textarea)(?=[\t >]|$)|\/?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|track|ul)(?=[\t >]|\/>|$))/i;

function paragraph(text: string, field: keyof typeof labels): Span | undefined {
  const rows = lines(text);
  const index = rows.findIndex(line => !line.fenced && line.text.startsWith(`**${labels[field]}:**`));
  if (index < 0) return;
  const first = rows[index];
  const source = visibleSource(text.slice(first.start, first.end - first.eol.length));
  const prefix = new RegExp(`^\\*\\*${labels[field]}:\\*\\*[ \\t]*`).exec(source.visible)![0];
  let last = first;
  for (const line of rows.slice(index + 1)) {
    if (htmlParagraphBoundary.test(line.text)) break;
    if (!line.text.trim() || /^(\*\*[^*\r\n]+:\*\*| {0,3}(?:>|#{1,6}(?:[ \t]|$)|[-+*][ \t]+|1[.)][ \t]+|(?:-[ \t]*){3,}$|(?:\*[ \t]*){3,}$|(?:_[ \t]*){3,}$|`{3,}|~{3,}))/.test(line.text)) break;
    last = line;
  }
  const start = first.start + source.offsets[prefix.length];
  const end = Math.max(start, last.contentEnd);
  return { start, end, after: last.end - last.eol.length, value: withoutComments(text.slice(start, end)).trimEnd() };
}

function matching(text: string, pattern: RegExp): Line[] {
  return lines(text).filter(line => !line.fenced && line.text.trim() && pattern.test(line.text));
}
function verifySpan(text: string): Span | undefined {
  const rows = lines(text);
  const index = rows.findIndex(line => !line.fenced && /^ {0,3}`{3,}[^`]*$/.test(line.text));
  if (index < 0) return;
  const opening = rows[index];
  const width = opening.text.trimStart().match(/^`+/)![0].length;
  const closing = rows.slice(index + 1).find(line => new RegExp(`^ {0,3}\x60{${width},}[ \\t]*$`).test(line.text));
  const end = closing?.start ?? text.length;
  return { start: opening.end, end, value: text.slice(opening.end, end) };
}
const estimatePattern = /^Estimate: (.*?) min · Depends on: (.*)$/;

export function fieldOf<K extends Field>(form: TicketForm, field: K): Fields[K] {
  const text = field === "title" ? form.preamble : form.sections.find(section => section.name === sections[field as Exclude<Field, "title">])?.text ?? "";
  let value: Fields[Field];
  if (field === "title") value = matching(text, /^# /)[0]?.text.slice(2) ?? "";
  else if (field === "summary" || field === "notes" || field === "openQuestions") value = visibleSource(text).visible.trim();
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
  const comments = visibleSource(removed).comments.join("");
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
    const { visible, offsets } = visibleSource(text);
    if (!visible.trim()) return append(text, String(value), eol);
    const start = offsets[visible.length - visible.trimStart().length];
    const end = offsets[visible.trimEnd().length - 1] + 1;
    return splice(text, start, end, String(value));
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
    return span ? text.slice(0, span.start) + (commands ? commands + eol : "") + text.slice(span.end) : append(text, "```" + eol + (commands ? commands + eol : "") + "```", eol);
  }
  const match = matching(text, estimatePattern)[0]?.text.match(estimatePattern);
  const estimate = field === "estimate" ? value ?? "" : match?.[1] ?? "";
  const dependencies = field === "dependsOn" ? (value as string[]).join(", ") || "none" : match?.[2] ?? "none";
  return replaceLine(text, estimatePattern, `Estimate: ${estimate} min · Depends on: ${dependencies}`, eol);
}

/** `form` with only acceptance criterion `index`'s text set to `value`,
 *  every other line of its section kept where it stands. */
export function withCriterion(form: TicketForm, index: number, value: string): TicketForm {
  const at = form.sections.findIndex(section => section.name === sections.criteria);
  const row = at < 0 ? undefined : matching(form.sections[at].text, itemPatterns.criteria)[index];
  if (!row) return form;
  const updated = form.sections.slice();
  updated[at] = { ...updated[at], text: splice(updated[at].text, row.start, row.contentEnd, `- [ ] ${value}`) };
  return { ...form, sections: updated };
}

/** Each acceptance criterion's served block in `text`: its line, then the
 *  indented lines under it up to the next unindented one. */
function criterionBlocks(text: string) {
  const heads = matching(text, itemPatterns.criteria);
  const all = lines(text);
  return heads.map((head, index) => {
    const first = all.findIndex(line => line.start === head.start);
    let last = first;
    for (let next = first + 1; next < all.length && all[next].start !== heads[index + 1]?.start; next++) {
      if (!all[next].text.trim()) continue;
      if (!/^[ \t]/.test(all[next].text)) break;
      last = next;
    }
    return { head, start: head.start, headEnd: all[first].end, end: all[last].end };
  });
}

/** Each acceptance criterion's indented detail, as served under its line;
 *  "" for a criterion with none. */
export function criterionDetails(form: TicketForm): string[] {
  const text = form.sections.find(section => section.name === sections.criteria)?.text ?? "";
  return criterionBlocks(text).map(block => text.slice(block.headEnd, block.end).trimEnd());
}

/** `form` with its acceptance criteria rebuilt as `rows`: a row with an
 *  `origin` carries that criterion's whole served block, its line and the
 *  indented lines under it, retitled to `text` and, when `detail` is given,
 *  with those lines replaced by it; a row without one is new. */
export function withCriteria(form: TicketForm, rows: { origin: number | null; text: string; detail?: string }[]): TicketForm {
  const at = form.sections.findIndex(section => section.name === sections.criteria);
  const text = at < 0 ? "" : form.sections[at].text;
  const blocks = criterionBlocks(text);
  if (!blocks.length) return withField(form, "criteria", rows.map(row => row.text));
  const eol = composeBody(form).includes("\r\n") ? "\r\n" : "\n";
  const gaps = blocks.slice(1).map((block, index) => text.slice(blocks[index].end, block.start));
  const separator = gaps.find(gap => !gap.trim()) ?? "";
  const line = (piece: string) => piece.endsWith("\n") ? piece : piece + eol;
  const rebuilt = rows.map(row => {
    if (row.origin === null) return `- [ ] ${row.text}${eol}`;
    const { head, start, headEnd, end } = blocks[row.origin];
    const title = line(splice(text.slice(start, headEnd), 0, head.contentEnd - start, `- [ ] ${row.text}`));
    if (row.detail === undefined) return title + text.slice(headEnd, end);
    return title + (row.detail.trim() ? line(row.detail) : "");
  }).map(line).join(separator);
  const end = blocks[blocks.length - 1].end;
  const tail = text.slice(end);
  const updated = form.sections.slice();
  updated[at] = { ...updated[at], text: text.slice(0, blocks[0].start) + (end === text.length && !text.endsWith("\n") ? rebuilt.replace(/\r?\n$/, "") : rebuilt) + gaps.filter(gap => gap.trim()).join("") + tail };
  return { ...form, sections: updated };
}

export function withField<K extends Field>(form: TicketForm, field: K, value: Fields[K]): TicketForm {
  const present = field === "title" || form.sections.some(section => section.name === sections[field as Exclude<Field, "title">]);
  if (present && JSON.stringify(fieldOf(form, field)) === JSON.stringify(value)) return form;
  const eol = composeBody(form).includes("\r\n") ? "\r\n" : "\n";
  if (field === "title") {
    const row = matching(form.preamble, /^#(?: |$)/)[0];
    const end = row ? Math.min(row.end - row.eol.length, Math.max(row.start + 2, row.contentEnd)) : 0;
    const preamble = row ? splice(form.preamble, row.start, end, `# ${value}`) : `# ${value}${eol}${eol}` + form.preamble;
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

/** The estimate in minutes, 5 to 120 in steps of 5: the run's time box. */
export const clampEstimate = (value: number) => Math.min(120, Math.max(5, Math.round(value / 5) * 5));

export function blankForm(): TicketForm {
  const optional = new Set(["Reproduce", "Mock-up", "Contract checks", "Evidence", "Story"]);
  const hints: TicketForm["hints"] = {};
  const uncommented = withoutComments(TEMPLATE);
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
  const form = parseBody(uncommented.replace(/^>.*(?:\r?\n|$)/gm, "").replace(/<[^>]*>/g, "").replace("{{TITLE}}", ""));
  form.sections = form.sections.filter(section => !optional.has(section.name));
  return { ...withField(withField(form, "verify", []), "openQuestions", "- None"), hints };
}

/** Each field's name as the form labels it, in the order the form draws it. */
export const FIELD_NAMES: [keyof Omit<EditFields, "others" | "criteriaFrom" | "criteriaDetail">, string][] = [
  ["title", "Title"], ["summary", "Summary"], ["what", "What"], ["why", "Why"], ["how", "How"],
  ["inScope", "In scope"], ["outOfScope", "Out of scope"], ["criteria", "Acceptance criteria"],
  ["verify", "Verify commands"], ["estimate", "Estimate"], ["priority", "Priority"], ["dependsOn", "Depends on"],
  ["uiChange", "UI change"], ["mockupLink", "Mock-up link"], ["mockupApproval", "Approval"], ["mockupImages", "Boards"],
  ["notes", "Implementation notes"],
];

/** A field both sides changed differently, with each side's text. */
interface Clash { name: string; theirs: string; mine: string }
export interface EditMerge { onlyTheirs: string[]; onlyMine: string[]; both: Clash[]; onTop: EditFields }

/** The `index` of a section the edit puts back: in place of the section of
 *  its name that the body draws as fields, else at the body's end. */
export const ADDED_SECTION = -3;

type Criterion = { text: string; detail: string } | undefined;
const criterionAt = (form: EditFields, index: number): Criterion =>
  index < form.criteria.length ? { text: form.criteria[index]!, detail: form.criteriaDetail[index] ?? "" } : undefined;

/** Each of `form`'s other sections by its key: the preamble, the unlabelled
 *  What / Why / How text, or a section's name with how many before it in
 *  the body share it, the one drawn as fields among them. */
export function keyed(form: EditFields): Map<string, EditFields["others"][number]> {
  const seen = new Map<string, number>();
  return new Map(form.others.map(other => {
    if (other.index < 0) return [`${other.index}`, other];
    const count = seen.get(other.name) ?? (other.repeated ? 1 : 0);
    seen.set(other.name, count + 1);
    return [`${other.name}#${count}`, other];
  }));
}

function shown(value: unknown): string {
  if (value === undefined) return "(removed)";
  if (value === null) return "none";
  if (Array.isArray(value)) return value.join("\n");
  if (typeof value === "object") {
    const { text, detail } = value as { text: string; detail: string };
    return detail ? `${text}\n${detail}` : text;
  }
  return String(value);
}

/** `form`'s text for the section `name` as its fields draw it, labelled
 *  when several fields share it; undefined when none of them holds text. */
function drawnText(form: EditFields, name: string): string | undefined {
  const fields = FIELD_NAMES.filter(([field]) => (field === "title" ? "Title" : (sections as Record<string, string>)[field]) === name);
  const parts = fields.map(([field, label]) => [label, field === "criteria" ? form.criteria.map((_, index) => shown(criterionAt(form, index))).join("\n") : shown(form[field])]).filter(([, text]) => text && text !== "none");
  if (!parts.length) return undefined;
  return fields.length === 1 ? parts[0]![1] : parts.map(([label, text]) => `${label}: ${text}`).join("\n");
}

/** `mine`, an edit of `base`, set against `theirs`, a newer revision of it,
 *  field by field: each criterion by its position, each other section by
 *  its name. `onTop` is `theirs` with every field `mine` changed set to
 *  `mine`'s text. */
export function mergeEdit(base: EditFields, theirs: EditFields, mine: EditFields): EditMerge {
  const merge: EditMerge = { onlyTheirs: [], onlyMine: [], both: [], onTop: { ...theirs } };
  const judge = (name: string, was: unknown, their: unknown, my: unknown): boolean => {
    const theyChanged = !same(was, their);
    const iChanged = !same(was, my);
    if (theyChanged && !iChanged) merge.onlyTheirs.push(name);
    else if (iChanged && !theyChanged) merge.onlyMine.push(name);
    else if (iChanged && !same(their, my)) merge.both.push({ name, theirs: shown(their), mine: shown(my) });
    return iChanged;
  };
  for (const [field, name] of FIELD_NAMES) {
    if (field !== "criteria") {
      if (judge(name, base[field], theirs[field], mine[field])) merge.onTop = { ...merge.onTop, [field]: mine[field] };
      continue;
    }
    const criteria: { from: number | null; text: string; detail: string }[] = [];
    const count = Math.max(base.criteria.length, theirs.criteria.length, mine.criteria.length);
    for (let index = 0; index < count; index += 1) {
      const kept = judge(`AC${index + 1}`, criterionAt(base, index), criterionAt(theirs, index), criterionAt(mine, index)) ? criterionAt(mine, index) : criterionAt(theirs, index);
      if (kept) criteria.push({ from: theirs.criteriaFrom[index] ?? null, ...kept });
    }
    merge.onTop = { ...merge.onTop, criteria: criteria.map(each => each.text), criteriaFrom: criteria.map(each => each.from), criteriaDetail: criteria.map(each => each.detail) };
  }
  const [was, their, my] = [keyed(base), keyed(theirs), keyed(mine)];
  merge.onTop = {
    ...merge.onTop,
    others: [...their].map(([key, other]) => {
      const text = my.get(key)?.text;
      return judge(other.name, was.get(key)?.text, other.text, text) && text !== undefined ? { ...other, text } : other;
    }),
  };
  for (const [key, other] of was) {
    const text = my.get(key)?.text;
    if (their.has(key) || text === undefined) continue;
    if (judge(other.name, other.text, other.index === -1 || other.repeated ? undefined : drawnText(theirs, other.name), text)) merge.onTop.others = restored(merge.onTop.others, { ...other, text }, theirs);
  }
  for (const [key, other] of my) {
    if (was.has(key) || their.has(key)) continue;
    merge.onlyMine.push(other.name);
    merge.onTop.others = restored(merge.onTop.others, other, theirs);
  }
  return merge;
}

/** `others` with `other`, a section `theirs` lacks, put back: unlabelled
 *  What / Why / How text joins that section when `theirs` draws it as plain
 *  text and becomes its own section when `theirs` has none. */
function restored(others: EditFields["others"], other: EditFields["others"][number], theirs: EditFields): EditFields["others"] {
  if (other.index !== -1) return [...others, { ...other, index: other.index < 0 ? other.index : ADDED_SECTION }];
  const plain = others.findIndex(each => each.index >= 0 && each.name === other.name && !each.repeated);
  if (plain >= 0) return others.map((each, index) => index === plain ? { ...each, text: `${each.text}\n\n${other.text}` } : each);
  return [...others, theirs.what || theirs.why || theirs.how || theirs.uiChange ? other : { ...other, index: ADDED_SECTION }];
}
