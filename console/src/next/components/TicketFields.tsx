import { useEffect, useState, type ReactNode } from "react";
import { checkTicket, type CheckResult } from "../../lib/boardWrites";
import type { PollDeps } from "../../lib/poll";
import { placeMessage, WHOLE_TICKET } from "../checker";
import { boardColumn, COLUMN_HEADS, type TicketCard } from "../tickets";
import { clampEstimate } from "../ticketForm";
import { Btn } from "./Btn";
import { TONE_TEXT } from "./StateDot";
import type { Tone } from "../state";

export const INPUT = "mt-2 w-full min-w-0 rounded border border-rule bg-canvas px-3 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-act";
/** The 2 px bar and wash of a field whose text differs from the revision the page loaded. */
export const CHANGED = "border-l-2 border-l-act bg-act-wash";
type Message = { text: string; error: boolean; field: string };
const CHECK_DELAY_MS = 600;
export const messagesId = (field: string) => `check-${field.replace(/\W+/g, "-")}`;

/** The checker's judgement of `body` once it has stood unedited for
 *  `CHECK_DELAY_MS`, judged as an edit of `ticket` when it is named. An
 *  edit aborts the request in flight, so one check runs at a time and an
 *  answer for an older body is never shown. `null` while `body` is,
 *  `undefined` until first answered. */
export function useTicketCheck(base: string | undefined, body: string | null, deps: PollDeps, ticket?: string): CheckResult | null | undefined {
  const [answer, setAnswer] = useState<{ base: string; body: string; result: CheckResult } | null>(null);
  useEffect(() => {
    if (body === null || base == null) return setAnswer(null);
    const controller = new AbortController();
    const cancel = deps.timer(() => void checkTicket(base, body, deps.fetch, controller.signal, ticket).then(result => {
      if (!controller.signal.aborted) setAnswer({ base, body, result });
    }), CHECK_DELAY_MS);
    return () => {
      cancel();
      controller.abort();
    };
  }, [base, body, deps, ticket]);
  if (body === null) return null;
  return answer && answer.base === base && answer.body === body ? answer.result : undefined;
}

/** A check's messages placed on the fields they name, with the
 *  `aria-invalid` and `aria-describedby` marks each field's input takes. */
export function checkMarks(check: CheckResult | null | undefined) {
  const judged = typeof check === "object" && check !== null && !("error" in check) ? check : null;
  const messages: Message[] = judged ? [
    ...judged.problems.map(text => ({ text, error: true, field: placeMessage(text) })),
    ...judged.advisories.map(text => ({ text, error: false, field: placeMessage(text) })),
  ] : [];
  const messagesOf = (field: string) => messages.filter(message => message.field === field);
  const marks = (fields: string | string[], className: string) => {
    const owners = [fields].flat().filter(field => messagesOf(field).length);
    const invalid = owners.some(field => messagesOf(field).some(message => message.error));
    return {
      "aria-invalid": invalid || undefined,
      "aria-describedby": owners.length ? owners.map(messagesId).join(" ") : undefined,
      className: invalid ? className.replace("border-rule", "border-bad") : className,
    };
  };
  return { judged, messages, messagesOf, marks };
}
type Marks = ReturnType<typeof checkMarks>;

const fieldId = (field: string) => field.startsWith("AC") ? `criterion-${Number(field.slice(2)) - 1}`
  : field === "Acceptance criteria" ? "criterion-0" : field === "Estimate" ? "estimate" : `new-${field}`;

export function Messages({ id, messages }: { id: string; messages: Message[] }) {
  if (!messages.length) return null;
  return <ul id={id} className="mt-2 space-y-1 font-mono text-[12px] [overflow-wrap:anywhere]">
    {messages.map((message, index) => <li key={index} className={message.error ? "text-bad" : "text-ask"}>
      <span aria-hidden="true" className="mr-1.5">{message.error ? "×" : "!"}</span>{message.text}
    </li>)}
  </ul>;
}

export function Field({ label, optional = false, hint, extra, messages = [], changed = false, children }: { label: string; optional?: boolean; hint?: string; extra?: string; messages?: Message[]; changed?: boolean; children: ReactNode }) {
  return (
    <div data-changed={changed || undefined} className={`min-w-0 ${changed ? CHANGED : ""}`}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <label htmlFor={`new-${label}`} className="font-['IBM_Plex_Sans'] text-[14px] font-semibold text-ink">{label}</label>
        <span className="font-mono text-[10.5px] uppercase text-ink-3">{optional ? "Optional" : "Required"}{extra}</span>
      </div>
      {hint && <p className="mt-1 text-[13px] text-ink-2">{hint}</p>}
      {children}
      <Messages id={messagesId(label)} messages={messages} />
    </div>
  );
}

/** The acceptance criteria as one input a row, reorderable and removable;
 *  `changed` marks the rows whose text differs from the loaded revision.
 *  `onChange` names, for each new row, the index of the row it was, or
 *  null for a row added. A row whose `details` entry is a string draws
 *  that criterion's indented detail as its own field. */
export function CriteriaField({ criteria, onChange, marks, changed = () => false, details = [], onDetail, detailChanged = () => false }: {
  criteria: string[]; onChange: (next: string[], from: (number | null)[]) => void; marks: Marks; changed?: (index: number) => boolean;
  details?: (string | null)[]; onDetail?: (index: number, detail: string) => void; detailChanged?: (index: number) => boolean;
}) {
  const change = (from: (number | null)[], next = from.map(index => index === null ? "" : criteria[index]!)) =>
    from.length ? onChange(next, from) : onChange([""], [null]);
  const rows = criteria.map((_, index): number | null => index);
  const move = (index: number, delta: number) => {
    const next = [...rows];
    [next[index], next[index + delta]] = [next[index + delta]!, next[index]!];
    change(next);
  };
  return <Field label="Acceptance criteria" extra=" · each one a reviewer can witness" hint="Screenshots and clips go under Evidence on the run, not here." messages={marks.messagesOf("Acceptance criteria")}>
    <div className="mt-3 space-y-3">
      {criteria.map((criterion, index) => <div key={index} data-changed={changed(index) || undefined} className={`flex flex-wrap items-center gap-2 border-b border-rule-2 pb-3 ${changed(index) ? `${CHANGED} pl-2 pt-2` : ""}`}>
        <label htmlFor={`criterion-${index}`} className="w-8 font-mono text-[11px] text-ink-3">AC{index + 1}</label>
        <input id={`criterion-${index}`} {...marks.marks([`AC${index + 1}`, "Acceptance criteria"], `${INPUT} !mt-0 flex-1 basis-[160px]`)} value={criterion} onChange={event => change(rows, criteria.map((value, at) => at === index ? event.target.value : value))} />
        <div className="ml-auto flex gap-1">
          <Btn className="!w-10 !h-10 !p-0" aria-label={`Move AC${index + 1} up`} disabled={index === 0} onClick={() => move(index, -1)}>↑</Btn>
          <Btn className="!w-10 !h-10 !p-0" aria-label={`Move AC${index + 1} down`} disabled={index === criteria.length - 1} onClick={() => move(index, 1)}>↓</Btn>
          <Btn className="!w-10 !h-10 !p-0" aria-label={`Remove AC${index + 1}`} onClick={() => change(rows.filter((_, at) => at !== index))}>×</Btn>
        </div>
        {details[index] != null && <div data-changed={detailChanged(index) || undefined} className={`basis-full pl-10 ${detailChanged(index) ? `${CHANGED} py-2` : ""}`}>
          <textarea aria-label={`AC${index + 1} detail`} rows={2} className={`${INPUT} !mt-0 resize-y font-mono text-[13px]`} value={details[index]!} onChange={event => onDetail?.(index, event.target.value)} />
        </div>}
        <div className="basis-full empty:hidden"><Messages id={messagesId(`AC${index + 1}`)} messages={marks.messagesOf(`AC${index + 1}`)} /></div>
      </div>)}
    </div>
    <div className="mt-3 flex flex-wrap items-center gap-3">
      <Btn onClick={() => change([...rows, null])}>+ Add a criterion</Btn>
      <span className="font-mono text-[12px] text-ink-3">{criteria.filter(value => value.trim()).length} of {criteria.length} written</span>
    </div>
  </Field>;
}

export function EstimateField({ draft, onDraft, estimate, marks, changed = false }: { draft: string; onDraft: (draft: string) => void; estimate: number; marks: Marks; changed?: boolean }) {
  const change = (value: number) => {
    if (Number.isFinite(value)) onDraft(String(clampEstimate(value)));
  };
  return <div data-changed={changed || undefined} className={changed ? `${CHANGED} pl-2` : undefined}>
    <label htmlFor="estimate" className="text-[14px] font-semibold">Estimate <span className="ml-2 font-mono text-[10.5px] font-normal uppercase text-ink-3">Minutes</span></label>
    <div className="mt-2 flex items-center">
      <Btn aria-label="Decrease estimate" disabled={estimate <= 5} onClick={() => change(estimate - 5)}>–</Btn>
      <input id="estimate" aria-label="Estimate" type="number" min={5} max={120} step={5} value={draft} onChange={event => onDraft(event.target.value)} onBlur={() => change(estimate)} {...marks.marks("Estimate", "h-11 w-16 border border-rule bg-canvas text-center font-mono text-ink")} />
      <Btn aria-label="Increase estimate" disabled={estimate >= 120} onClick={() => change(estimate + 5)}>+</Btn>
    </div>
    <p className="mt-2 font-mono text-[12px] text-ink-3">sets the run's time box · cap 90</p>
    <Messages id={messagesId("Estimate")} messages={marks.messagesOf("Estimate")} />
  </div>;
}

export function PriorityField({ priority, onChange, changed = false }: { priority: number | null; onChange: (priority: number) => void; changed?: boolean }) {
  return <div role="group" aria-label="Priority" data-changed={changed || undefined} className={changed ? `${CHANGED} pl-2` : undefined}>
    <p className="text-[14px] font-semibold">Priority</p>
    <div className="mt-2 flex flex-wrap">
      {[0, 1, 2, 3, 4].map(value => <Btn key={value} className="!px-2" aria-pressed={priority === value} variant={priority === value ? "primary" : "quiet"} onClick={() => onChange(value)}>
        {value > 0 && <span aria-hidden="true" className="mr-1.5 h-2 w-2" style={{ background: `var(--${value === 4 ? "ink-3" : `p${value}`})` }} />}{value === 0 ? "None" : `P${value}`}
      </Btn>)}
    </div>
    <p className="mt-2 font-mono text-[12px] text-ink-3">P1 is the highest</p>
  </div>;
}

/** The keys the ticket waits on, each with its board column when `cards`
 *  holds it, and the box `dependency` that adds one on Enter or Add. */
export function DependsField({ dependsOn, onChange, dependency, onDependency, cards, marks, changed = false }: { dependsOn: string[]; onChange: (next: string[]) => void; dependency: string; onDependency: (value: string) => void; cards: TicketCard[]; marks: Marks; changed?: boolean }) {
  const add = () => {
    const key = dependency.trim().toUpperCase();
    if (key && !dependsOn.includes(key)) onChange([...dependsOn, key]);
    onDependency("");
  };
  return <Field label="Depends on" optional messages={marks.messagesOf("Depends on")} changed={changed}>
    <div className="mt-2 flex flex-wrap gap-2">
      {dependsOn.map(key => {
        const card = cards.find(card => card.key === key);
        return <span key={key} className="inline-flex max-w-full items-center gap-2 border border-rule bg-canvas pl-2 font-mono text-[12px]">
          <span className="min-w-0 [overflow-wrap:anywhere]">{key}{card ? ` · ${COLUMN_HEADS[boardColumn(card)].label}` : ""}</span>
          <Btn className="!w-8 !h-8 !p-0 shrink-0" aria-label={`Remove ${key}`} onClick={() => onChange(dependsOn.filter(value => value !== key))}>×</Btn>
        </span>;
      })}
    </div>
    <div className="mt-2 flex gap-2">
      <input id="new-Depends on" {...marks.marks("Depends on", `${INPUT} !mt-0`)} value={dependency} onChange={event => onDependency(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); add(); } }} />
      <Btn onClick={add}>Add</Btn>
    </div>
  </Field>;
}

/** The Checker panel: the summary line, then each message, a button that
 *  focuses the field it names. */
export function CheckerPanel({ summary, marks }: { summary: { text: string; tone: Tone }; marks: Marks }) {
  const { messages } = marks;
  return <section aria-label="Checker" className="mb-4 border border-rule bg-paper p-5">
    <div className="flex items-baseline justify-between gap-2 border-b border-rule-2 pb-2">
      <h2 className="font-mono text-[10.5px] font-semibold uppercase tracking-[0.08em] text-ink-2">Checker</h2>
      <span className="font-mono text-[11px] text-ink-3">as you type</span>
    </div>
    <p aria-live="polite" className={`mt-3 font-mono text-[13px] [overflow-wrap:anywhere] ${TONE_TEXT[summary.tone]}`}>{summary.text}</p>
    {messages.length > 0 && <ul aria-label="Checker messages" className="mt-3 space-y-1">{messages.map((message, index) => {
      const row = <>
        <span aria-hidden="true" className={`mr-1.5 ${message.error ? "text-bad" : "text-ask"}`}>{message.error ? "×" : "!"}</span>
        <span className="mr-1.5 font-mono text-[12px] font-semibold text-ink">{message.field}</span>
        <span className="text-[13px] text-ink-2">{message.text}</span>
      </>;
      return <li key={index} className="[overflow-wrap:anywhere]">{message.field === WHOLE_TICKET ? <div className="py-1">{row}</div> :
        <button type="button" className="w-full py-1 text-left hover:bg-canvas focus:outline-act" onClick={() => document.getElementById(fieldId(message.field))?.focus()}>{row}</button>}</li>;
    })}</ul>}
  </section>;
}
