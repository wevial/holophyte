import type { CheckResult } from "../lib/boardWrites";

/** The ticket as a whole: a message no field owns. */
export const WHOLE_TICKET = "Ticket";

const PLACES: [RegExp, string][] = [
  [/H1 title/, "Title"],
  [/(?:acceptance criterion #|Acceptance criteria #|criterion )(\d+)\b/, "AC"],
  [/'Acceptance criteria'/, "Acceptance criteria"],
  [/'Summary'/, "Summary"],
  [/'\*\*What:\*\*'/, "What"],
  [/'\*\*How:\*\*'/, "How"],
  [/'\*\*UI change:\*\*'/, "UI change"],
  [/'## Mock-up'/, "Mock-up link"],
  [/'In scope'/, "In scope"],
  [/'Out of scope'/, "Out of scope"],
  [/verify command|'Verify command\(s\)'/, "Verify commands"],
  [/estimate is|'Estimate & dependencies'/, "Estimate"],
  [/'Depends on'/, "Depends on"],
  [/Implementation notes/, "Implementation notes"],
];

/** The New ticket field a checker message belongs to, named as the form
 *  labels it ("Summary", "AC2", "Mock-up link"), read from the checker's
 *  own wording; `WHOLE_TICKET` when no field owns it. */
export function placeMessage(text: string): string {
  for (const [pattern, field] of PLACES) {
    const match = pattern.exec(text);
    if (match) return field === "AC" ? `AC${match[1]}` : field;
  }
  return WHOLE_TICKET;
}

export type Tone = "bad" | "live" | "ink-3";

const count = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** The panel's summary line and its tone for a check's state: `null` for a
 *  blank form, `undefined` while the first answer is awaited. */
export function checkSummary(check: CheckResult | null | undefined): { text: string; tone: Tone } {
  if (check === null) return { text: "Nothing to check yet. Start with a title.", tone: "ink-3" };
  if (check === undefined) return { text: "Checking…", tone: "ink-3" };
  if (check === "unsupported") return { text: "This host does not check as you type; File ticket still checks.", tone: "ink-3" };
  if ("error" in check) return { text: `The checker is not answering: ${check.error}`, tone: "bad" };
  const advisories = count(check.advisories.length, "advisory", "advisories");
  if (check.problems.length) {
    const errors = `${count(check.problems.length, "error blocks", "errors block")} filing`;
    return { text: check.advisories.length ? `${errors} · ${advisories}` : errors, tone: "bad" };
  }
  if (check.advisories.length) return { text: `Ready · ${advisories}, none blocking`, tone: "live" };
  return { text: "Ready. Nothing to fix.", tone: "live" };
}

/** The note under File ticket: why it is held, or where it files. */
export function fileNote(blank: boolean, errors: number, project: string): string {
  if (blank) return "Fill in the template to file.";
  if (errors) return `Fix ${count(errors, "error", "errors")} to file.`;
  return `Files to ${project} Backlog.`;
}
