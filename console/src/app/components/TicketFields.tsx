import { useEffect, useState, type ReactNode } from "react";
import { checkTicket, type CheckResult } from "../../lib/boardWrites";
import type { PollDeps } from "../../lib/poll";
import { placeMessage, WHOLE_TICKET } from "../checker";
import { boardColumn, COLUMN_HEADS, type TicketCard } from "../tickets";
import { clampEstimate, type Evidence, type Fields } from "../ticketForm";
import { Btn } from "./Btn";
import { Picker } from "./Picker";
import { Segmented } from "./Segmented";
import { TONE_TEXT } from "./StateDot";
import type { Tone } from "../state";

export const INPUT = "field mt-2 w-full min-w-0";
const SMALL = "!h-7 !rounded-[7px] !px-2.5 !text-[12.5px]";
const ICON_BUTTON = "grid size-7 shrink-0 cursor-pointer place-items-center rounded-[6px] text-ink-2 transition-colors duration-150 hover:bg-hover hover:text-ink disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent motion-reduce:transition-none";
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
      className,
    };
  };
  return { judged, messages, messagesOf, marks };
}
type Marks = ReturnType<typeof checkMarks>;

const evidenceField = /^E(\d+)(?: (File|State))?$/;
const fieldId = (field: string) => field.startsWith("AC") ? `criterion-${Number(field.slice(2)) - 1}`
  : evidenceField.test(field) ? field.replace(evidenceField, (_, row: string, box = "file") => `evidence-${box.toLowerCase()}-${Number(row) - 1}`)
  : field === "Acceptance criteria" ? "criterion-0" : field === "Estimate" ? "estimate" : `new-${field}`;

export function Messages({ id, messages }: { id: string; messages: Message[] }) {
  if (!messages.length) return null;
  return <ul id={id} className="mt-2 space-y-1 font-sans text-[12.5px] [overflow-wrap:anywhere]">
    {messages.map((message, index) => <li key={index} className={message.error ? "text-bad" : "text-ask-ink"}>
      <span aria-hidden="true" className="mr-1.5">{message.error ? "×" : "!"}</span>{message.text}
    </li>)}
  </ul>;
}

/** The accent dot of a field or row whose text differs from the revision the page loaded. */
function ChangedDot({ changed }: { changed: boolean }) {
  return changed ? <span aria-hidden="true" className="size-1.5 shrink-0 self-center rounded-full bg-accent" /> : null;
}

function RowIcon({ d }: { d: string }) {
  return <svg aria-hidden="true" viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round"><path d={d} /></svg>;
}
const UP = "M8 12.5v-9M4.5 7 8 3.5 11.5 7";
const DOWN = "M8 3.5v9M4.5 9 8 12.5 11.5 9";
const CROSS = "m4.5 4.5 7 7m0-7-7 7";
const MINUS = "M4 8h8";
const PLUS = "M8 4v8M4 8h8";

function RowButtons({ name, index, count, onMove, onRemove }: { name: string; index: number; count: number; onMove: (delta: number) => void; onRemove: () => void }) {
  return <div className="ml-auto flex gap-0.5">
    <button type="button" className={ICON_BUTTON} aria-label={`Move ${name} up`} disabled={index === 0} onClick={() => onMove(-1)}><RowIcon d={UP} /></button>
    <button type="button" className={ICON_BUTTON} aria-label={`Move ${name} down`} disabled={index === count - 1} onClick={() => onMove(1)}><RowIcon d={DOWN} /></button>
    <button type="button" className={ICON_BUTTON} aria-label={`Remove ${name}`} onClick={onRemove}><RowIcon d={CROSS} /></button>
  </div>;
}

const HEAD = "font-sans text-[13px] font-semibold text-ink";

function FieldHead({ label, htmlFor, aside, changed }: { label: string; htmlFor?: string; aside?: string; changed: boolean }) {
  return <div className="flex flex-wrap items-baseline gap-x-2">
    <ChangedDot changed={changed} />
    {htmlFor ? <label htmlFor={htmlFor} className={HEAD}>{label}</label> : <span className={HEAD}>{label}</span>}
    {aside && <span className="font-sans text-[13px] text-ink-3">{aside}</span>}
  </div>;
}

export function Field({ label, optional = false, hint, extra, messages = [], changed = false, children }: { label: string; optional?: boolean; hint?: string; extra?: string; messages?: Message[]; changed?: boolean; children: ReactNode }) {
  const aside = `${optional ? "Optional" : ""}${extra ?? ""}`.replace(/^ · /, "").trim();
  return (
    <div data-changed={changed || undefined} className="min-w-0">
      <FieldHead label={label} htmlFor={`new-${label}`} aside={aside} changed={changed} />
      {hint && <p className="mt-1 font-sans text-[13px] text-ink-3">{hint}</p>}
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
    <div className="mt-2">
      {criteria.map((criterion, index) => <div key={index} data-changed={changed(index) || undefined} className="flex flex-wrap items-center gap-2 border-b border-hair py-2.5">
        <span className="flex w-10 shrink-0 items-center gap-1.5"><ChangedDot changed={changed(index)} /><label htmlFor={`criterion-${index}`} className="font-mono text-[11.5px] text-ink-3">AC{index + 1}</label></span>
        <input id={`criterion-${index}`} {...marks.marks([`AC${index + 1}`, "Acceptance criteria"], `${INPUT} !mt-0 flex-1 basis-[160px]`)} value={criterion} onChange={event => change(rows, criteria.map((value, at) => at === index ? event.target.value : value))} />
        <RowButtons name={`AC${index + 1}`} index={index} count={criteria.length} onMove={delta => move(index, delta)} onRemove={() => change(rows.filter((_, at) => at !== index))} />
        {details[index] != null && <div data-changed={detailChanged(index) || undefined} className="flex basis-full items-start gap-1.5 pl-[18px]">
          <span className="mt-3 w-4 shrink-0"><ChangedDot changed={detailChanged(index)} /></span>
          <textarea aria-label={`AC${index + 1} detail`} rows={2} className={`${INPUT} !mt-0 flex-1 resize-y font-mono text-[13px]`} value={details[index]!} onChange={event => onDetail?.(index, event.target.value)} />
        </div>}
        <div className="basis-full empty:hidden"><Messages id={messagesId(`AC${index + 1}`)} messages={marks.messagesOf(`AC${index + 1}`)} /></div>
      </div>)}
    </div>
    <div className="mt-3 flex flex-wrap items-center gap-3">
      <Btn className={SMALL} onClick={() => change([...rows, null])}>+ Add a criterion</Btn>
      <span className="font-mono text-[12px] text-ink-3">{criteria.filter(value => value.trim()).length} of {criteria.length} written</span>
    </div>
  </Field>;
}

const UI_CHANGES: [Fields["uiChange"], string][] = [[null, "None"], ["minor", "Minor"], ["major", "Major"]];
const BLAST_RADII: [Fields["blastRadius"], string][] = [[null, "Diff decides"], ["medium", "Medium"], ["high", "High"]];

const OUTSIDE = "\u0000outside";

function Choice<T extends string | null>({ label, options, value, onChange, marks, changed, hint, optional }: {
  label: string; options: [T, string][]; value: T; onChange: (value: T) => void; marks: Marks; changed: boolean; hint?: string; optional?: boolean;
}) {
  const outside = options.some(([option]) => option === value) ? [] : [{ value: OUTSIDE, label: `${value} · not a choice` }];
  const { className: _, ...aria } = marks.marks(label, "");
  return <Field label={label} optional={optional} hint={hint} messages={marks.messagesOf(label)} changed={changed}>
    <div className="mt-2">
      <Picker id={`new-${label}`} label={label} {...aria} value={outside.length ? OUTSIDE : value ?? ""}
        options={[...options.map(([option, name]) => ({ value: option ?? "", label: name })), ...outside]}
        onPick={picked => {
          const chosen = options.find(([option]) => (option ?? "") === picked);
          if (chosen) onChange(chosen[0]);
        }} />
    </div>
  </Field>;
}

export function UiChangeField({ uiChange, onChange, marks, changed = false }: { uiChange: Fields["uiChange"]; onChange: (value: Fields["uiChange"]) => void; marks: Marks; changed?: boolean }) {
  return <Choice label="UI change" options={UI_CHANGES} value={uiChange} onChange={onChange} marks={marks} changed={changed} />;
}

export function BlastRadiusField({ blastRadius, onChange, marks, changed = false }: { blastRadius: Fields["blastRadius"]; onChange: (value: Fields["blastRadius"]) => void; marks: Marks; changed?: boolean }) {
  return <Choice label="Blast radius" optional hint="Raises the review tier when a change is riskier than its diff." options={BLAST_RADII} value={blastRadius} onChange={onChange} marks={marks} changed={changed} />;
}

const MAX_EVIDENCE = 6;
const position = (index: number) => String(index + 1).padStart(2, "0");
const renumbered = (rows: Evidence[]) => rows.map((row, index) => /^\d{2}-/.test(row.file) ? { ...row, file: position(index) + row.file.slice(2) } : row);

/** The `[[evidence]]` entries as one row each of a file and the state it
 *  captures, reorderable and removable; a move or removal renumbers each
 *  file that opens with its two-digit position. */
export function EvidenceField({ evidence, onChange, marks, changed = () => false }: { evidence: Evidence[]; onChange: (next: Evidence[]) => void; marks: Marks; changed?: (index: number) => boolean }) {
  const move = (index: number, delta: number) => {
    const next = [...evidence];
    [next[index], next[index + delta]] = [next[index + delta]!, next[index]!];
    onChange(renumbered(next));
  };
  const edit = (index: number, part: Partial<Evidence>) => onChange(evidence.map((row, at) => at === index ? { ...row, ...part } : row));
  return <Field label="Evidence" optional extra=" · the states the run captures" hint="Each a file named NN-slug.png, or NN-slug.webm for a flow, and the state it shows." messages={marks.messagesOf("Evidence")}>
    <div className="mt-2 empty:hidden">
      {evidence.map((row, index) => {
        const name = `E${index + 1}`;
        return <div key={index} data-changed={changed(index) || undefined} className="flex flex-wrap items-end gap-2 border-b border-hair py-2.5">
          <span className="flex h-8 w-10 shrink-0 items-center gap-1.5"><ChangedDot changed={changed(index)} /><span className="font-mono text-[11.5px] text-ink-3">{name}</span></span>
          <label className="min-w-0 flex-1 basis-[150px] font-sans text-[12.5px] text-ink-3">File
            <input id={`evidence-file-${index}`} aria-label={`${name} File`} placeholder={`${position(index)}-slug.png`} {...marks.marks([`${name} File`, name], `${INPUT} !mt-1 font-mono`)} value={row.file} onChange={event => edit(index, { file: event.target.value })} />
          </label>
          <label className="min-w-0 flex-[2] basis-[200px] font-sans text-[12.5px] text-ink-3">State
            <input id={`evidence-state-${index}`} aria-label={`${name} State`} placeholder="the page and state it shows" {...marks.marks([`${name} State`, name], `${INPUT} !mt-1 font-sans`)} value={row.state} onChange={event => edit(index, { state: event.target.value })} />
          </label>
          <span className="flex h-8 items-center"><RowButtons name={name} index={index} count={evidence.length} onMove={delta => move(index, delta)} onRemove={() => onChange(renumbered(evidence.filter((_, at) => at !== index)))} /></span>
          <div className="basis-full empty:hidden">
            {[name, `${name} File`, `${name} State`].map(field => <Messages key={field} id={messagesId(field)} messages={marks.messagesOf(field)} />)}
          </div>
        </div>;
      })}
    </div>
    <div className="mt-3 flex flex-wrap items-center gap-3">
      <Btn className={SMALL} disabled={evidence.length >= MAX_EVIDENCE} onClick={() => onChange([...evidence, { file: `${position(evidence.length)}-`, state: "" }])}>+ Add a state</Btn>
      <span className="font-mono text-[12px] text-ink-3">{evidence.length} of {MAX_EVIDENCE}</span>
    </div>
  </Field>;
}

export function EstimateField({ draft, onDraft, estimate, marks, changed = false }: { draft: string; onDraft: (draft: string) => void; estimate: number; marks: Marks; changed?: boolean }) {
  const change = (value: number) => {
    if (Number.isFinite(value)) onDraft(String(clampEstimate(value)));
  };
  return <div data-changed={changed || undefined}>
    <FieldHead label="Estimate" htmlFor="estimate" aside="Minutes" changed={changed} />
    <div className="mt-2 inline-flex items-center rounded-full bg-selected p-0.5 has-[[aria-invalid=true]]:shadow-[inset_0_0_0_1px_var(--bad)] has-[input:focus-visible]:shadow-[0_0_0_2px_var(--focus)]">
      <button type="button" className={`${ICON_BUTTON} rounded-full`} aria-label="Decrease estimate" disabled={estimate <= 5} onClick={() => change(estimate - 5)}><RowIcon d={MINUS} /></button>
      <input id="estimate" aria-label="Estimate" type="number" min={5} max={120} step={5} value={draft} onChange={event => onDraft(event.target.value)} onBlur={() => change(estimate)} {...marks.marks("Estimate", "h-7 w-12 border-0 bg-transparent text-center font-mono text-[13px] tabular-nums text-ink outline-none! [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none")} />
      <button type="button" className={`${ICON_BUTTON} rounded-full`} aria-label="Increase estimate" disabled={estimate >= 120} onClick={() => change(estimate + 5)}><RowIcon d={PLUS} /></button>
    </div>
    <p className="mt-2 font-sans text-[13px] text-ink-3">sets the run's time box · cap 90</p>
    <Messages id={messagesId("Estimate")} messages={marks.messagesOf("Estimate")} />
  </div>;
}

const PRIORITIES = [0, 1, 2, 3, 4].map(value => ({ value: String(value), label: value === 0 ? "None" : `P${value}` }));

export function PriorityField({ priority, onChange, changed = false }: { priority: number | null; onChange: (priority: number) => void; changed?: boolean }) {
  return <div data-changed={changed || undefined}>
    <FieldHead label="Priority" changed={changed} />
    <div className="mt-2">
      <Segmented label="Priority" options={PRIORITIES} value={priority == null ? undefined : String(priority)} onPick={picked => onChange(Number(picked))} />
    </div>
    <p className="mt-2 font-sans text-[13px] text-ink-3">P1 is the highest</p>
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
        return <span key={key} className="inline-flex min-h-7 max-w-full items-center gap-1 rounded-full bg-selected py-0.5 pr-0.5 pl-2.5 text-ink">
          <span className="min-w-0 font-mono text-[12px] [overflow-wrap:anywhere]">{key}{card ? ` · ${COLUMN_HEADS[boardColumn(card)].label}` : ""}</span>
          <button type="button" className={`${ICON_BUTTON} !size-6 rounded-full`} aria-label={`Remove ${key}`} onClick={() => onChange(dependsOn.filter(value => value !== key))}><RowIcon d={CROSS} /></button>
        </span>;
      })}
    </div>
    <div className="mt-2 flex gap-2">
      <input id="new-Depends on" {...marks.marks("Depends on", `${INPUT} !mt-0`)} value={dependency} onChange={event => onDependency(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); add(); } }} />
      <Btn className={`${SMALL} self-center`} onClick={add}>Add</Btn>
    </div>
  </Field>;
}

/** The Checker panel: the summary line, then each message, a button that
 *  focuses the field it names. */
export function CheckerPanel({ summary, marks }: { summary: { text: string; tone: Tone }; marks: Marks }) {
  const { messages } = marks;
  return <section aria-label="Checker" className="mb-4 rounded-[10px] bg-raised px-3.5 pt-1 pb-3 shadow-[inset_0_0_0_1px_var(--hair)]">
    <h2 className="flex h-[30px] items-center border-b border-hair font-sans text-[13px] font-semibold text-ink">Checker</h2>
    <p aria-live="polite" className={`mt-3 font-sans text-[13px] [overflow-wrap:anywhere] ${TONE_TEXT[summary.tone]}`}>{summary.text}</p>
    {messages.length > 0 && <ul aria-label="Checker messages" className="mt-3 space-y-1">{messages.map((message, index) => {
      const row = <>
        <span aria-hidden="true" className={`mr-1.5 ${message.error ? "text-bad" : "text-ask-ink"}`}>{message.error ? "×" : "!"}</span>
        <span className="mr-1.5 font-mono text-[12px] font-semibold text-ink">{message.field}</span>
        <span className="text-[13px] text-ink-2">{message.text}</span>
      </>;
      return <li key={index} className="[overflow-wrap:anywhere]">{message.field === WHOLE_TICKET ? <div className="px-1.5 py-1">{row}</div> :
        <button type="button" className="w-full rounded-[6px] px-1.5 py-1 text-left hover:bg-hover focus:outline-focus" onClick={() => document.getElementById(fieldId(message.field))?.focus()}>{row}</button>}</li>;
    })}</ul>}
  </section>;
}
