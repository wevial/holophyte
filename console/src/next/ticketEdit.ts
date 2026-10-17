import { clampEstimate, composeBody, fieldOf, parseBody, withField, type TicketForm } from "./ticketForm";

/** The sections the edit form draws as fields; every other section of the
 *  body is edited as plain text under "Other sections". */
const DRAWN = new Set(["Summary", "What / Why / How", "In scope", "Out of scope", "Acceptance criteria", "Verify command(s)", "Estimate & dependencies", "Implementation notes", "Mock-up"]);

const PROSE = ["title", "summary", "what", "why", "how", "inScope", "outOfScope", "verify", "notes", "mockupLink", "mockupApproval", "mockupImages"] as const;
type Prose = typeof PROSE[number];

/** A ticket revision as the edit form holds it: each list field one item a
 *  line, the estimate as typed, and the undrawn sections by their index in
 *  the body. */
export type EditFields = Record<Prose, string> & {
  criteria: string[];
  estimate: string;
  priority: number | null;
  dependsOn: string[];
  uiChange: "major" | "minor" | null;
  others: { index: number; name: string; text: string }[];
};

/** Each field's name as the form labels it, in the order the form draws it. */
const FIELD_NAMES: [keyof Omit<EditFields, "others">, string][] = [
  ["title", "Title"], ["summary", "Summary"], ["what", "What"], ["why", "Why"], ["how", "How"],
  ["inScope", "In scope"], ["outOfScope", "Out of scope"], ["criteria", "Acceptance criteria"],
  ["verify", "Verify commands"], ["estimate", "Estimate"], ["priority", "Priority"], ["dependsOn", "Depends on"],
  ["uiChange", "UI change"], ["mockupLink", "Mock-up link"], ["mockupApproval", "Approval"], ["mockupImages", "Boards"],
  ["notes", "Implementation notes"],
];

const imageUrl = (image: string) => image.replace(/^!\[[^\]]*\]\((.*)\)$/, "$1");

export function loadEdit(body: string, priority: number | null): EditFields {
  const form = parseBody(body);
  const prose = Object.fromEntries(PROSE.map(field => {
    const value = field === "mockupImages" ? fieldOf(form, field).map(imageUrl) : fieldOf(form, field);
    return [field, Array.isArray(value) ? value.join("\n") : value];
  })) as Record<Prose, string>;
  const estimate = fieldOf(form, "estimate");
  return {
    ...prose,
    criteria: fieldOf(form, "criteria").length ? fieldOf(form, "criteria") : [""],
    estimate: estimate == null ? "" : String(estimate),
    priority,
    dependsOn: fieldOf(form, "dependsOn"),
    uiChange: fieldOf(form, "uiChange"),
    others: form.sections.flatMap((section, index) => DRAWN.has(section.name) ? [] : [{ index, name: section.name, text: section.text.trim() }]),
  };
}

/** Whether `body` has a Mock-up section, whose fields the form draws even with no UI change. */
export const hasMockup = (body: string) => parseBody(body).sections.some(section => section.name === "Mock-up");

export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/** The names of the fields `edited` holds differently from `loaded`. */
export function changedNames(loaded: EditFields, edited: EditFields): string[] {
  const fields = FIELD_NAMES.filter(([field]) => !same(loaded[field], edited[field])).map(([, name]) => name);
  const others = edited.others.filter((other, at) => other.text !== loaded.others[at]?.text).map(other => other.name);
  return [...fields, ...others];
}

const lines = (text: string) => (text ? text.split("\n") : []);

function withText(text: string, value: string): string {
  if (!text.trim()) return `\n\n${value}\n\n`;
  return text.slice(0, text.length - text.trimStart().length) + value + text.slice(text.trimEnd().length);
}

/** `body` with only the fields `edited` changed from `loaded` rewritten, so
 *  every line no edit touched is kept as it was served. */
export function composeEdit(body: string, loaded: EditFields, edited: EditFields): string {
  let form: TicketForm = parseBody(body);
  form = {
    ...form,
    sections: form.sections.map((section, index) => {
      const at = edited.others.findIndex(other => other.index === index);
      return at < 0 || edited.others[at]!.text === loaded.others[at]?.text ? section : { ...section, text: withText(section.text, edited.others[at]!.text) };
    }),
  };
  for (const [field] of FIELD_NAMES) {
    if (same(loaded[field], edited[field])) continue;
    if (field === "inScope" || field === "outOfScope" || field === "verify") form = withField(form, field, lines(edited[field]));
    else if (field === "mockupImages") form = withField(form, field, lines(edited[field]).filter(url => url.trim()).map(url => `![board](${url.trim()})`));
    else if (field === "estimate") form = withField(form, field, edited.estimate.trim() === "" ? null : clampEstimate(Number(edited.estimate) || 25));
    else if (field !== "priority") form = withField(form, field, edited[field]);
  }
  return composeBody(form);
}
