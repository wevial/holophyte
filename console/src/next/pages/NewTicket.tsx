import { useContext, useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { checkTicket, fileTicket, type CheckResult } from "../../lib/boardWrites";
import { projectName } from "../../lib/derive";
import type { PollDeps } from "../../lib/poll";
import { checkSummary, fileNote, placeMessage, WHOLE_TICKET } from "../checker";
import { Btn } from "../components/Btn";
import { TONE_TEXT } from "../components/StateDot";
import { useToast } from "../components/Toast";
import { switcherRows } from "../projects";
import { useBoards, useReadiness } from "../queries";
import { PollDepsContext, useConsolePeers } from "../scope";
import { blankForm, composeBody, fieldOf, withField } from "../ticketForm";
import { boardColumn, COLUMN_HEADS, hostCards } from "../tickets";
import { CRUMB_LINK } from "./runShared";

const INPUT = "mt-2 w-full min-w-0 rounded border border-rule bg-canvas px-3 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-act";
const FIELDS = ["title", "summary", "what", "why", "how", "inScope", "outOfScope", "verify", "notes"] as const;
type TextField = typeof FIELDS[number];
type Message = { text: string; error: boolean; field: string };
const CHECK_DELAY_MS = 600;
const messagesId = (field: string) => `check-${field.replace(/\W+/g, "-")}`;

/** The checker's judgement of `body` once it has stood unedited for
 *  `CHECK_DELAY_MS`. An edit aborts the request in flight, so one check
 *  runs at a time and an answer for an older body is never shown.
 *  `null` while the form is blank, `undefined` until first answered. */
function useTicketCheck(base: string | undefined, body: string | null, deps: PollDeps): CheckResult | null | undefined {
  const [answer, setAnswer] = useState<{ base: string; result: CheckResult } | null>(null);
  useEffect(() => {
    if (body === null || base == null) return setAnswer(null);
    const controller = new AbortController();
    const cancel = deps.timer(() => void checkTicket(base, body, deps.fetch, controller.signal).then(result => {
      if (!controller.signal.aborted) setAnswer({ base, result });
    }), CHECK_DELAY_MS);
    return () => {
      cancel();
      controller.abort();
    };
  }, [base, body, deps]);
  if (body === null) return null;
  return answer && answer.base === base ? answer.result : undefined;
}

function Messages({ id, messages }: { id: string; messages: Message[] }) {
  if (!messages.length) return null;
  return <ul id={id} className="mt-2 space-y-1 font-mono text-[12px] [overflow-wrap:anywhere]">
    {messages.map((message, index) => <li key={index} className={message.error ? "text-bad" : "text-ask"}>
      <span aria-hidden="true" className="mr-1.5">{message.error ? "×" : "!"}</span>{message.text}
    </li>)}
  </ul>;
}

function Field({ label, optional = false, hint, extra, messages = [], children }: { label: string; optional?: boolean; hint?: string; extra?: string; messages?: Message[]; children: ReactNode }) {
  return (
    <div className="min-w-0">
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

export function NewTicket() {
  const { project } = useParams({ from: "/tickets/$project/new" });
  const peers = useConsolePeers();
  const deps = useContext(PollDepsContext);
  const boards = useBoards(peers.hosts, deps);
  const editable = switcherRows(peers.hosts, peers.now, new Map()).flatMap(row => {
    const match = boards.find(({ host, board }) => board.editable && host.project != null && projectName(host.project) === row.name);
    return match && row.name != null ? [{ name: row.name, base: match.host.base }] : [];
  });
  const target = editable.find(row => row.name === project);
  const readiness = useReadiness(peers.hosts.filter(host => host.base === target?.base), deps);
  const answered = boards.some(({ host }) => host.project != null && projectName(host.project) === project);
  const [template, setTemplate] = useState(() => {
    const form = blankForm();
    form.sections = form.sections.filter(section => section.name !== "Acceptance criteria" && section.name !== "Estimate & dependencies");
    return withField(form, "estimate", 25);
  });
  const [priority, setPriority] = useState(2);
  const [dependency, setDependency] = useState("");
  const [mockupLink, setMockupLink] = useState("");
  const [mockupBoards, setMockupBoards] = useState("");
  const [approval, setApproval] = useState(() => `Approved ${new Date().toISOString().slice(0, 10)}: `);
  const [criteria, setCriteria] = useState([""]);
  const [estimateDraft, setEstimateDraft] = useState("25");
  const estimate = Math.min(120, Math.max(5, Math.round((Number(estimateDraft) || 25) / 5) * 5));
  const dependsOn = fieldOf(template, "dependsOn");
  const uiChange = fieldOf(template, "uiChange");
  const cards = boards.filter(({ host }) => host.base === target?.base).flatMap(({ host, board }) => hostCards(host, board, readiness, peers.now));
  const changeCriteria = (next: string[]) => setCriteria(next.length ? next : [""]);
  const moveCriterion = (index: number, delta: number) => {
    const next = [...criteria];
    [next[index], next[index + delta]] = [next[index + delta]!, next[index]!];
    changeCriteria(next);
  };
  const changeEstimate = (value: number) => {
    if (Number.isFinite(value)) setEstimateDraft(String(Math.min(120, Math.max(5, Math.round(value / 5) * 5))));
  };
  const addDependency = () => {
    const key = dependency.trim().toUpperCase();
    if (key && !dependsOn.includes(key)) setTemplate(previous => withField(previous, "dependsOn", [...dependsOn, key]));
    setDependency("");
  };
  const toggleUI = () => setTemplate(previous => withField(previous, "uiChange", fieldOf(previous, "uiChange") ? null : "major"));
  const [values, setValues] = useState(() => Object.fromEntries(FIELDS.map(field => [field, ""])) as Record<TextField, string>);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const navigate = useNavigate();
  const toast = useToast();
  const client = useQueryClient();
  let form = withField(withField(template, "criteria", criteria), "estimate", estimate);
  for (const field of FIELDS) {
    if (field === "inScope" || field === "outOfScope" || field === "verify") form = withField(form, field, values[field] ? values[field].split("\n") : []);
    else if (field === "notes") form = withField(form, field, values[field].split("\n").filter(line => line.trim()).map(line => `- ${line}`).join("\n"));
    else form = withField(form, field, values[field]);
  }
  if (uiChange) {
    form = withField(withField(withField(form, "mockupLink", mockupLink), "mockupApproval", approval), "mockupImages", mockupBoards.split("\n").filter(url => url.trim()).map(url => `![board](${url.trim()})`));
  }
  const body = composeBody(form);
  const blank = !values.title.trim() && !values.summary.trim() && !values.what.trim();
  const check = useTicketCheck(target?.base, blank ? null : body, deps);
  const judged = typeof check === "object" && check !== null && !("error" in check) ? check : null;
  const messages: Message[] = judged ? [
    ...judged.problems.map(text => ({ text, error: true, field: placeMessage(text) })),
    ...judged.advisories.map(text => ({ text, error: false, field: placeMessage(text) })),
  ] : [];
  const errors = judged?.problems.length ?? 0;
  const held = blank || errors > 0;
  const summary = checkSummary(check);
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
  const fieldId = (field: string) => field.startsWith("AC") ? `criterion-${Number(field.slice(2)) - 1}`
    : field === "Acceptance criteria" ? "criterion-0" : field === "Estimate" ? "estimate" : `new-${field}`;
  const input = (field: TextField, label: string, hint?: string, optional = false) => {
    const placeholder = template.hints[field];
    const props = {
      id: `new-${label}`, value: values[field], placeholder: Array.isArray(placeholder) ? placeholder.join("\n") : placeholder,
      onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setValues(previous => ({ ...previous, [field]: event.target.value })),
    };
    return <Field label={label} hint={hint} optional={optional} messages={messagesOf(label)} extra={field === "verify" ? " · one a line, run as written" : undefined}>
      {field === "title" ? <input {...props} {...marks(label, `${INPUT} font-sans text-[18px] font-semibold`)} /> :
        <textarea {...props} rows={field === "summary" || field === "what" || field === "why" ? 2 : 3} {...marks(label, `${INPUT} resize-y ${field === "verify" ? "font-mono" : "font-sans"}`)} />}
    </Field>;
  };
  const file = async () => {
    if (!target || busy || held) return;
    setBusy(true);
    setProblems([]);
    const result = await fileTicket(target.base, body, priority, deps.fetch, "backlog");
    setBusy(false);
    if (!result.ok) {
      setProblems("problems" in result ? result.problems : [result.error]);
      return;
    }
    toast(`Filed ${result.ticket} in ${project} Backlog.`);
    void client.invalidateQueries({ queryKey: ["fleet"] });
    void client.invalidateQueries({ queryKey: ["route", target.base] });
    void navigate({ to: "/tickets/$project/$key", params: { project, key: result.ticket }, search: {} });
  };
  return (
    <div className="px-6 pb-8 pt-5 max-[720px]:px-4">
      <section aria-labelledby="page-title" className="mx-auto max-w-[1180px]">
        <nav aria-label="Breadcrumb" className="font-mono text-[12px] text-ink-3">
          <Link to="/tickets" search={{}} className={CRUMB_LINK}>Tickets</Link>{" / "}
          <Link to="/tickets" search={{ project }} className={CRUMB_LINK}>{project}</Link>{" / new"}
        </nav>
        <header className="mt-5 border-b border-rule pb-[18px]">
          <h1 id="page-title" className="text-[24px] font-semibold text-ink">New ticket</h1>
          <p className="mt-1.5 font-mono text-[13px] text-ink-2">{project} · from the ticket template · lands in Backlog</p>
        </header>
        {!target ? answered ? <div className="mt-6 font-mono text-[13px] text-ink-2">
          <p>{project} takes no tickets from the console: its board is on Linear, or its host has actions off.</p>
          <Link to="/tickets" search={{ project }} className="mt-3 inline-block text-act underline">Back to Tickets</Link>
        </div> : <p className="mt-6 text-ink-3">Loading project board…</p> : <>
          <div className="mt-6 grid min-w-0 items-start gap-6 min-[1100px]:grid-cols-[minmax(0,1fr)_300px]">
            <div className="min-w-0 divide-y divide-rule-2 border border-rule bg-paper [&>div]:p-5">
              <Field label="Project">
                <select id="new-Project" className={`${INPUT} font-sans`} value={project} disabled={busy} onChange={event => {
                  setProblems([]);
                  void navigate({ to: "/tickets/$project/new", params: { project: event.target.value }, search: {} });
                }}>{editable.map(row => <option key={row.name} value={row.name}>{row.name}</option>)}</select>
              </Field>
              {input("title", "Title", "The outcome, as the board card says it.")}
              {input("summary", "Summary", "One or two sentences. Shown under the title on the card and in peek.")}
              <div className="grid gap-5 min-[720px]:grid-cols-2">
                {input("what", "What")}{input("why", "Why", undefined, true)}
              </div>
              {input("how", "How")}
              {input("inScope", "In scope", "One a line.")}
              {input("outOfScope", "Out of scope", "One a line.")}
              <Field label="Acceptance criteria" extra=" · each one a reviewer can witness" hint="Screenshots and clips go under Evidence on the run, not here." messages={messagesOf("Acceptance criteria")}>
                <div className="mt-3 space-y-3">
                  {criteria.map((criterion, index) => <div key={index} className="flex flex-wrap items-center gap-2 border-b border-rule-2 pb-3">
                    <label htmlFor={`criterion-${index}`} className="w-8 font-mono text-[11px] text-ink-3">AC{index + 1}</label>
                    <input id={`criterion-${index}`} {...marks([`AC${index + 1}`, "Acceptance criteria"], `${INPUT} !mt-0 flex-1 basis-[160px]`)} value={criterion} onChange={event => changeCriteria(criteria.map((value, at) => at === index ? event.target.value : value))} />
                    <div className="ml-auto flex gap-1">
                      <Btn className="!w-10 !h-10 !p-0" aria-label={`Move AC${index + 1} up`} disabled={index === 0} onClick={() => moveCriterion(index, -1)}>↑</Btn>
                      <Btn className="!w-10 !h-10 !p-0" aria-label={`Move AC${index + 1} down`} disabled={index === criteria.length - 1} onClick={() => moveCriterion(index, 1)}>↓</Btn>
                      <Btn className="!w-10 !h-10 !p-0" aria-label={`Remove AC${index + 1}`} onClick={() => changeCriteria(criteria.filter((_, at) => at !== index))}>×</Btn>
                    </div>
                    <div className="basis-full empty:hidden"><Messages id={messagesId(`AC${index + 1}`)} messages={messagesOf(`AC${index + 1}`)} /></div>
                  </div>)}
                </div>
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <Btn onClick={() => changeCriteria([...criteria, ""])}>+ Add a criterion</Btn>
                  <span className="font-mono text-[12px] text-ink-3">{criteria.filter(value => value.trim()).length} of {criteria.length} written</span>
                </div>
              </Field>
              {input("verify", "Verify commands", "The implementer runs these before review; a red one fails the run.")}
              <div className="flex flex-wrap gap-6">
                <div>
                  <label htmlFor="estimate" className="text-[14px] font-semibold">Estimate <span className="ml-2 font-mono text-[10.5px] font-normal uppercase text-ink-3">Minutes</span></label>
                  <div className="mt-2 flex items-center">
                    <Btn aria-label="Decrease estimate" disabled={estimate <= 5} onClick={() => changeEstimate(estimate - 5)}>–</Btn>
                    <input id="estimate" aria-label="Estimate" type="number" min={5} max={120} step={5} value={estimateDraft} onChange={event => setEstimateDraft(event.target.value)} onBlur={() => changeEstimate(estimate)} {...marks("Estimate", "h-11 w-16 border border-rule bg-canvas text-center font-mono text-ink")} />
                    <Btn aria-label="Increase estimate" disabled={estimate >= 120} onClick={() => changeEstimate(estimate + 5)}>+</Btn>
                  </div>
                  <p className="mt-2 font-mono text-[12px] text-ink-3">sets the run's time box · cap 90</p>
                  <Messages id={messagesId("Estimate")} messages={messagesOf("Estimate")} />
                </div>
                <div role="group" aria-label="Priority">
                  <p className="text-[14px] font-semibold">Priority</p>
                  <div className="mt-2 flex flex-wrap">
                    {[0, 1, 2, 3, 4].map(value => <Btn key={value} className="!px-2" aria-pressed={priority === value} variant={priority === value ? "primary" : "quiet"} onClick={() => setPriority(value)}>
                      {value > 0 && <span aria-hidden="true" className="mr-1.5 h-2 w-2" style={{ background: `var(--${value === 4 ? "ink-3" : `p${value}`})` }} />}{value === 0 ? "None" : `P${value}`}
                    </Btn>)}
                  </div>
                  <p className="mt-2 font-mono text-[12px] text-ink-3">P1 is the highest</p>
                </div>
                <Field label="Depends on" optional messages={messagesOf("Depends on")}>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {dependsOn.map(key => {
                      const card = cards.find(card => card.key === key);
                      return <span key={key} className="inline-flex max-w-full items-center gap-2 border border-rule bg-canvas pl-2 font-mono text-[12px]">
                        <span className="min-w-0 [overflow-wrap:anywhere]">{key}{card ? ` · ${COLUMN_HEADS[boardColumn(card)].label}` : ""}</span>
                        <Btn className="!w-8 !h-8 !p-0 shrink-0" aria-label={`Remove ${key}`} onClick={() => setTemplate(previous => withField(previous, "dependsOn", dependsOn.filter(value => value !== key)))}>×</Btn>
                      </span>;
                    })}
                  </div>
                  <div className="mt-2 flex gap-2">
                    <input id="new-Depends on" {...marks("Depends on", `${INPUT} !mt-0`)} value={dependency} onChange={event => setDependency(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); addDependency(); } }} />
                    <Btn onClick={addDependency}>Add</Btn>
                  </div>
                </Field>
              </div>
              <div className="space-y-5">
                <label className="flex items-center gap-3 text-[14px] font-semibold">
                  <input id="new-UI change" type="checkbox" role="switch" checked={uiChange !== null} onChange={toggleUI} {...marks("UI change", "h-5 w-9 accent-act")} />This ticket changes the UI
                </label>
                <Messages id={messagesId("UI change")} messages={messagesOf("UI change")} />
                {uiChange && <>
                  <Field label="Mock-up link" extra=" for a UI change" hint="The design the implementer builds to and the reviewer compares against." messages={messagesOf("Mock-up link")}>
                    <input id="new-Mock-up link" {...marks("Mock-up link", INPUT)} placeholder="https://…" value={mockupLink} onChange={event => setMockupLink(event.target.value)} />
                  </Field>
                  <Field label="Approval">
                    <input id="new-Approval" className={`${INPUT} font-mono`} value={approval} onChange={event => setApproval(event.target.value)} />
                  </Field>
                  <Field label="Boards" optional hint="One image link a line.">
                    <textarea id="new-Boards" rows={3} className={`${INPUT} font-mono`} value={mockupBoards} onChange={event => setMockupBoards(event.target.value)} />
                  </Field>
                </>}
              </div>
              {input("notes", "Implementation notes", "Files, helpers and constraints the implementer should know.", true)}
            </div>
            <aside aria-label="File ticket" className="min-w-0">
              <section aria-label="Checker" className="mb-4 border border-rule bg-paper p-5">
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
              </section>
              <div className="border border-rule bg-paper p-5">
                <Btn variant="primary" busy={busy} busyLabel="Filing…" disabled={held} onClick={() => void file()}>File ticket</Btn>
                <p className="mt-3 font-mono text-[12px] text-ink-2">{fileNote(blank, errors, project)}</p>
                {problems.length > 0 && <ul role="alert" className="mt-4 space-y-2 text-[13px] text-bad [overflow-wrap:anywhere]">{problems.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
              </div>
              <p className="mt-4 font-mono text-[12px] text-ink-3">From the ticket template. Errors block; advisories are the house style and do not.</p>
            </aside>
          </div>
          <footer className="mt-6 font-mono text-[12px] text-ink-3">a filed ticket lands in Backlog; move it to Ready when it is specced</footer>
        </>}
      </section>
    </div>
  );
}
