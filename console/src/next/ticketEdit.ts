import { ADDED_SECTION, clampEstimate, composeBody, criterionDetails, FIELD_NAMES, fieldOf, parseBody, withCriteria, withCriterion, withField, type TicketForm } from "./ticketForm";

const TITLE = "Title";
const nonblank = (text: string) => text.split("\n").map(line => line.replace(/\r$/, "")).filter(line => line.trim());
const listed = (text: string) => nonblank(text).every(line => /^- /.test(line));
const FENCE_OPEN = /^ {0,3}`{3,}[^`]*$/;
const FENCE_CLOSE = /^ {0,3}`{3,}[ \t]*$/;

/** The sections the edit form draws as fields, each with the shape its
 *  fields hold whole; a section of another shape, or holding a comment, is
 *  edited as plain text under "Other sections" like every undrawn one. */
const SHAPES: Record<string, (text: string) => boolean> = {
  "Summary": () => true,
  "What / Why / How": () => true,
  "In scope": listed,
  "Out of scope": listed,
  "Acceptance criteria": text => nonblank(text).every((line, index) => /^- \[ \] /.test(line) || (index > 0 && /^[ \t]/.test(line))),
  "Verify command(s)": text => {
    const rows = nonblank(text);
    return !rows.length || (rows.length >= 2 && FENCE_OPEN.test(rows[0]!) && FENCE_CLOSE.test(rows[rows.length - 1]!) && !rows.slice(1, -1).some(row => FENCE_OPEN.test(row)));
  },
  "Estimate & dependencies": text => nonblank(text).length <= 1 && nonblank(text).every(line => /^Estimate: .*? min · Depends on: .*$/.test(line)),
  "Implementation notes": () => true,
  "Mock-up": text => nonblank(text).every(line => /^https?:\/\/\S+[ \t]*$|^Approved \d{4}-\d{2}-\d{2}:|^!\[[^\]]*\]\(.*\)$/.test(line)),
};

/** The parts of `body` drawn as plain text because their fields cannot hold
 *  them whole: "Title" for a preamble with more than its title line, and
 *  each drawn section's name, Mock-up with What / Why / How, whose switch
 *  shows its fields. A drawn section's repeat is never drawn. */
export function rawParts(body: string): Set<string> {
  const form = parseBody(body);
  const raw = new Set<string>();
  const preamble = nonblank(form.preamble);
  if (form.preamble.includes("<!--") || preamble.length > 1 || (preamble.length === 1 && !/^# /.test(preamble[0]!))) raw.add(TITLE);
  for (const name of Object.keys(SHAPES)) {
    const section = form.sections.find(each => each.name === name);
    if (section && (section.text.includes("<!--") || !SHAPES[name]!(section.text))) raw.add(name);
  }
  if (raw.has("What / Why / How") && form.sections.some(section => section.name === "Mock-up")) raw.add("Mock-up");
  return raw;
}

/** Whether the section at `index` of `form` is drawn as fields. */
const drawn = (form: TicketForm, index: number, raw: Set<string>) => {
  const { name } = form.sections[index]!;
  return name in SHAPES && !raw.has(name) && form.sections.findIndex(section => section.name === name) === index;
};

const PROSE = ["title", "summary", "what", "why", "how", "inScope", "outOfScope", "verify", "notes", "mockupLink", "mockupApproval", "mockupImages"] as const;
type Prose = typeof PROSE[number];

/** A ticket revision as the edit form holds it: each list field one item a
 *  line, each criterion with the index it was served at (null when added)
 *  and the indented detail under it,
 *  the estimate as typed, and the undrawn sections by their index in the
 *  body, What / Why / How's unlabelled text at index -1. */
export type EditFields = Record<Prose, string> & {
  criteria: string[];
  criteriaFrom: (number | null)[];
  criteriaDetail: string[];
  estimate: string;
  priority: number | null;
  dependsOn: string[];
  uiChange: "major" | "minor" | null;
  others: { index: number; name: string; text: string }[];
};

const PROSE_SECTION = "What / Why / How";
const LABEL = /^\*\*(What|Why|How|UI change|Blast radius):\*\*[ \t]*/;
const LABELLED = { What: "what", Why: "why", How: "how" } as const;
type Segment = { label: string | null; start: number; value: number; end: number };

/** What / Why / How's `text` cut at each label line outside a fence: a
 *  label's text runs to the next label, so it may hold several paragraphs. */
function segments(text: string): Segment[] {
  const found: Segment[] = [{ label: null, start: 0, value: 0, end: text.length }];
  let fence = "";
  for (const line of text.matchAll(/[^\n]+|\n/g)) {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line[0]);
    if (marker) {
      if (!fence) fence = marker[1]!;
      else if (marker[1]![0] === fence[0] && marker[1]!.length >= fence.length && !marker[2]!.trim()) fence = "";
      continue;
    }
    const label = fence ? null : LABEL.exec(line[0]);
    if (!label) continue;
    found[found.length - 1]!.end = line.index;
    found.push({ label: label[1]!, start: line.index, value: line.index + label[0].length, end: text.length });
  }
  return found;
}

/** The segment each of What, Why and How is read from: its label's first. */
function owners(found: Segment[]): Map<Segment, "what" | "why" | "how"> {
  const owned = new Map<Segment, "what" | "why" | "how">();
  for (const [label, field] of Object.entries(LABELLED)) {
    const segment = found.find(each => each.label === label);
    if (segment) owned.set(segment, field);
  }
  return owned;
}

/** A segment no field draws, as the "What / Why / How" entry shows it: a
 *  UI change segment without its line, which the switch owns. */
const unowned = (text: string, segment: Segment) =>
  text.slice(segment.label === "UI change" ? (text.indexOf("\n", segment.start) + 1 || segment.end) : segment.start, segment.end);

const imageUrl = (image: string) => image.replace(/^!\[[^\]]*\]\((.*)\)$/, "$1");

export function loadEdit(body: string, priority: number | null): EditFields {
  const form = parseBody(body);
  const raw = rawParts(body);
  const prose = Object.fromEntries(PROSE.map(field => {
    const value = field === "mockupImages" ? fieldOf(form, field).map(imageUrl) : fieldOf(form, field);
    return [field, Array.isArray(value) ? value.join("\n") : value];
  })) as Record<Prose, string>;
  const prosed = form.sections.find(section => section.name === PROSE_SECTION)?.text ?? "";
  const found = segments(prosed);
  const owned = owners(found);
  for (const [segment, field] of owned) prose[field] = prosed.slice(segment.value, segment.end).trimEnd();
  const rest = found.filter(segment => !owned.has(segment)).map(segment => unowned(prosed, segment)).join("").trim();
  const estimate = fieldOf(form, "estimate");
  const criteria = fieldOf(form, "criteria");
  return {
    ...prose,
    criteria: criteria.length ? criteria : [""],
    criteriaFrom: criteria.length ? criteria.map((_, index) => index) : [null],
    criteriaDetail: criteria.length ? criterionDetails(form) : [""],
    estimate: estimate == null ? "" : String(estimate),
    priority,
    dependsOn: fieldOf(form, "dependsOn"),
    uiChange: fieldOf(form, "uiChange"),
    others: [
      ...(raw.has(TITLE) ? [{ index: -2, name: TITLE, text: form.preamble.trim() }] : []),
      ...(rest && !raw.has(PROSE_SECTION) ? [{ index: -1, name: PROSE_SECTION, text: rest }] : []),
      ...form.sections.flatMap((section, index) => drawn(form, index, raw) ? [] : [{ index, name: section.name, text: section.text.trim() }]),
    ],
  };
}

/** Whether `body` has a Mock-up section, whose fields the form draws even with no UI change. */
export const hasMockup = (body: string) => parseBody(body).sections.some(section => section.name === "Mock-up");

export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

const differs = (loaded: EditFields, edited: EditFields, field: keyof Omit<EditFields, "others" | "criteriaFrom" | "criteriaDetail">) =>
  !same(loaded[field], edited[field]) || (field === "criteria" && (!same(loaded.criteriaFrom, edited.criteriaFrom) || !same(loaded.criteriaDetail, edited.criteriaDetail)));

/** The names of the fields `edited` holds differently from `loaded`. */
export function changedNames(loaded: EditFields, edited: EditFields): string[] {
  const fields = FIELD_NAMES.filter(([field]) => differs(loaded, edited, field)).map(([, name]) => name);
  const others = edited.others.filter((other, at) => other.text !== loaded.others[at]?.text).map(other => other.name);
  return [...fields, ...others];
}

const lines = (text: string) => (text ? text.split("\n") : []);

function withText(text: string, value: string): string {
  if (!text.trim()) return `\n\n${value}\n\n`;
  return text.slice(0, text.length - text.trimStart().length) + value + text.slice(text.trimEnd().length);
}

/** What / Why / How's `text` with each of What, Why and How that changed
 *  rewritten in place, and, when the unlabelled text changed, that text
 *  moved to the section's end as `edited` holds it. */
function withProse(text: string, loaded: EditFields, edited: EditFields): string {
  const found = segments(text);
  const owned = owners(found);
  const restAt = edited.others.findIndex(other => other.index === -1);
  const rest = restAt < 0 ? null : edited.others[restAt]!.text;
  const restChanged = rest !== null && rest !== loaded.others[restAt]?.text;
  let result = found.map(segment => {
    const piece = text.slice(segment.start, segment.end);
    const field = owned.get(segment);
    if (field) {
      if (loaded[field] === edited[field]) return piece;
      return text.slice(segment.start, segment.value) + edited[field] + piece.slice(piece.trimEnd().length);
    }
    if (!restChanged) return piece;
    const kept = text.slice(segment.start, segment.end - unowned(text, segment).length);
    return kept + (unowned(text, segment).trim() ? "" : unowned(text, segment));
  }).join("");
  if (restChanged && rest!.trim()) {
    const trailing = result.slice(result.trimEnd().length) || "\n\n";
    result = `${result.trimEnd()}\n\n${rest}${trailing}`;
  }
  return result;
}

/** `body` with only the fields `edited` changed from `loaded` rewritten, so
 *  every line no edit touched is kept as it was served. */
export function composeEdit(body: string, loaded: EditFields, edited: EditFields): string {
  let form: TicketForm = parseBody(body);
  const raw = rawParts(body);
  const prosed = raw.has(PROSE_SECTION) ? undefined : form.sections.find(section => section.name === PROSE_SECTION);
  const drawnProse = new Set(prosed ? owners(segments(prosed.text)).values() : []);
  const title = edited.others.findIndex(other => other.index === -2);
  form = {
    ...form,
    preamble: title < 0 || edited.others[title]!.text === loaded.others[title]?.text ? form.preamble : withText(form.preamble, edited.others[title]!.text),
    sections: form.sections.map((section, index) => {
      if (section === prosed) return { ...section, text: withProse(section.text, loaded, edited) };
      const at = edited.others.findIndex(other => other.index === index);
      return at < 0 || edited.others[at]!.text === loaded.others[at]?.text ? section : { ...section, text: withText(section.text, edited.others[at]!.text) };
    }),
  };
  const eol = body.includes("\r\n") ? "\r\n" : "\n";
  for (const added of edited.others.filter(other => other.index === ADDED_SECTION)) {
    const last = form.sections.at(-1);
    const sections = last && !last.text.endsWith(eol + eol) ? [...form.sections.slice(0, -1), { ...last, text: last.text.replace(/\s*$/, eol + eol) }] : form.sections;
    form = { ...form, sections: [...sections, { name: added.name, heading: `## ${added.name}${eol}`, text: `${eol}${added.text}${eol}` }] };
  }
  for (const [field] of FIELD_NAMES) {
    if (!differs(loaded, edited, field)) continue;
    if (field === "inScope" || field === "outOfScope" || field === "verify") form = withField(form, field, lines(edited[field]));
    else if (field === "mockupImages") form = withField(form, field, lines(edited[field]).filter(url => url.trim()).map(url => `![board](${url.trim()})`));
    else if (field === "criteria") form = composeCriteria(form, loaded, edited);
    else if (field === "estimate") form = withField(form, field, edited.estimate.trim() === "" ? null : clampEstimate(Number(edited.estimate) || 25));
    else if ((field === "what" || field === "why" || field === "how") && drawnProse.has(field)) continue;
    else if (field !== "priority") form = withField(form, field, edited[field]);
  }
  return composeBody(form);
}

const indented = (detail: string) => detail.split("\n").map(line => !line.trim() || /^[ \t]/.test(line) ? line : `  ${line}`).join("\n");

/** Criteria still in their served places with their details as served are
 *  retitled line by line; any move, removal, addition or detail edit
 *  rebuilds the list from the served blocks. */
function composeCriteria(form: TicketForm, loaded: EditFields, edited: EditFields): TicketForm {
  const inPlace = edited.criteria.length === loaded.criteria.length && edited.criteriaFrom.every((from, index) => from === index);
  if (inPlace && same(edited.criteriaDetail, loaded.criteriaDetail)) {
    edited.criteria.forEach((criterion, index) => {
      if (criterion !== loaded.criteria[index]) form = withCriterion(form, index, criterion);
    });
    return form;
  }
  return withCriteria(form, edited.criteria.map((text, index) => {
    const origin = edited.criteriaFrom[index] ?? null;
    const detail = edited.criteriaDetail[index] ?? "";
    return { origin, text, detail: origin !== null && detail === loaded.criteriaDetail[origin] ? undefined : indented(detail) };
  }));
}
