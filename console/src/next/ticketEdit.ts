import { clampEstimate, composeBody, criterionDetails, fieldOf, parseBody, withCriteria, withCriterion, withField, type TicketForm } from "./ticketForm";

/** The sections the edit form draws as fields; every other section of the
 *  body is edited as plain text under "Other sections". */
const DRAWN = new Set(["Summary", "What / Why / How", "In scope", "Out of scope", "Acceptance criteria", "Verify command(s)", "Estimate & dependencies", "Implementation notes", "Mock-up"]);

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

/** Each field's name as the form labels it, in the order the form draws it. */
const FIELD_NAMES: [keyof Omit<EditFields, "others" | "criteriaFrom" | "criteriaDetail">, string][] = [
  ["title", "Title"], ["summary", "Summary"], ["what", "What"], ["why", "Why"], ["how", "How"],
  ["inScope", "In scope"], ["outOfScope", "Out of scope"], ["criteria", "Acceptance criteria"],
  ["verify", "Verify commands"], ["estimate", "Estimate"], ["priority", "Priority"], ["dependsOn", "Depends on"],
  ["uiChange", "UI change"], ["mockupLink", "Mock-up link"], ["mockupApproval", "Approval"], ["mockupImages", "Boards"],
  ["notes", "Implementation notes"],
];

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
      ...(rest ? [{ index: -1, name: PROSE_SECTION, text: rest }] : []),
      ...form.sections.flatMap((section, index) => DRAWN.has(section.name) ? [] : [{ index, name: section.name, text: section.text.trim() }]),
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
  const prosed = form.sections.find(section => section.name === PROSE_SECTION);
  const drawnProse = new Set(prosed ? owners(segments(prosed.text)).values() : []);
  form = {
    ...form,
    sections: form.sections.map((section, index) => {
      if (section === prosed) return { ...section, text: withProse(section.text, loaded, edited) };
      const at = edited.others.findIndex(other => other.index === index);
      return at < 0 || edited.others[at]!.text === loaded.others[at]?.text ? section : { ...section, text: withText(section.text, edited.others[at]!.text) };
    }),
  };
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
    return { origin, text, detail: origin !== null && detail === loaded.criteriaDetail[origin] ? undefined : detail };
  }));
}
