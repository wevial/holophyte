import { useContext, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { fileTicket } from "../../lib/boardWrites";
import { projectName } from "../../lib/derive";
import { Btn } from "../components/Btn";
import { useToast } from "../components/Toast";
import { switcherRows } from "../projects";
import { useBoards } from "../queries";
import { PollDepsContext, useConsolePeers } from "../scope";
import { blankForm, composeBody, fieldOf, withField } from "../ticketForm";
import { boardColumn, COLUMN_HEADS, hostCards } from "../tickets";
import { CRUMB_LINK } from "./runShared";

const INPUT = "mt-2 w-full min-w-0 rounded border border-rule bg-canvas px-3 py-2 text-[14px] text-ink placeholder:text-ink-3 focus:outline-act";
const FIELDS = ["title", "summary", "what", "why", "how", "inScope", "outOfScope", "verify", "notes"] as const;
type TextField = typeof FIELDS[number];

function Field({ label, optional = false, hint, extra, children }: { label: string; optional?: boolean; hint?: string; extra?: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <label htmlFor={`new-${label}`} className="font-['IBM_Plex_Sans'] text-[14px] font-semibold text-ink">{label}</label>
        <span className="font-mono text-[10.5px] uppercase text-ink-3">{optional ? "Optional" : "Required"}{extra}</span>
      </div>
      {hint && <p className="mt-1 text-[13px] text-ink-2">{hint}</p>}
      {children}
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
  const cards = boards.filter(({ host }) => host.base === target?.base).flatMap(({ host, board }) => hostCards(host, board, new Map(), peers.now));
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
  const toggleUI = () => setTemplate(previous => {
    if (fieldOf(previous, "uiChange")) {
      return withField(previous, "uiChange", null);
    }
    return withField(previous, "uiChange", "major");
  });
  const [values, setValues] = useState(() => Object.fromEntries(FIELDS.map(field => [field, ""])) as Record<TextField, string>);
  const [busy, setBusy] = useState(false);
  const [problems, setProblems] = useState<string[]>([]);
  const navigate = useNavigate();
  const toast = useToast();
  const client = useQueryClient();
  const input = (field: TextField, label: string, hint?: string, optional = false) => {
    const placeholder = template.hints[field];
    const props = {
      id: `new-${label}`, value: values[field], placeholder: Array.isArray(placeholder) ? placeholder.join("\n") : placeholder,
      onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setValues(previous => ({ ...previous, [field]: event.target.value })),
    };
    return <Field label={label} hint={hint} optional={optional} extra={field === "verify" ? " · one a line, run as written" : undefined}>
      {field === "title" ? <input {...props} className={`${INPUT} font-sans text-[18px] font-semibold`} /> :
        <textarea {...props} rows={field === "summary" || field === "what" || field === "why" ? 2 : 3} className={`${INPUT} resize-y ${field === "verify" ? "font-mono" : "font-sans"}`} />}
    </Field>;
  };
  const file = async () => {
    if (!target || busy) return;
    setBusy(true);
    setProblems([]);
    let form = withField(withField(template, "criteria", criteria.filter(value => value.trim())), "estimate", estimate);
    for (const field of FIELDS) {
      if (field === "inScope" || field === "outOfScope" || field === "verify") form = withField(form, field, values[field] ? values[field].split("\n") : []);
      else if (field === "notes") form = withField(form, field, values[field].split("\n").filter(line => line.trim()).map(line => `- ${line}`).join("\n"));
      else form = withField(form, field, values[field]);
    }
    if (uiChange) {
      form = withField(withField(withField(form, "mockupLink", mockupLink), "mockupApproval", approval), "mockupImages", mockupBoards.split("\n").filter(url => url.trim()).map(url => `![board](${url.trim()})`));
    }
    const result = await fileTicket(target.base, composeBody(form), priority, deps.fetch, "backlog");
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
              <Field label="Acceptance criteria" extra=" · each one a reviewer can witness" hint="Screenshots and clips go under Evidence on the run, not here.">
                <div className="mt-3 space-y-3">
                  {criteria.map((criterion, index) => <div key={index} className="flex flex-wrap items-center gap-2 border-b border-rule-2 pb-3">
                    <label htmlFor={`criterion-${index}`} className="w-8 font-mono text-[11px] text-ink-3">AC{index + 1}</label>
                    <input id={`criterion-${index}`} className={`${INPUT} !mt-0 flex-1 basis-[160px]`} value={criterion} onChange={event => changeCriteria(criteria.map((value, at) => at === index ? event.target.value : value))} />
                    <div className="ml-auto flex gap-1">
                      <Btn className="!w-10 !h-10 !p-0" aria-label={`Move AC${index + 1} up`} disabled={index === 0} onClick={() => moveCriterion(index, -1)}>↑</Btn>
                      <Btn className="!w-10 !h-10 !p-0" aria-label={`Move AC${index + 1} down`} disabled={index === criteria.length - 1} onClick={() => moveCriterion(index, 1)}>↓</Btn>
                      <Btn className="!w-10 !h-10 !p-0" aria-label={`Remove AC${index + 1}`} onClick={() => changeCriteria(criteria.filter((_, at) => at !== index))}>×</Btn>
                    </div>
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
                    <input id="estimate" aria-label="Estimate" type="number" min={5} max={120} step={5} value={estimateDraft} onChange={event => setEstimateDraft(event.target.value)} onBlur={() => changeEstimate(estimate)} className="h-11 w-16 border border-rule bg-canvas text-center font-mono text-ink" />
                    <Btn aria-label="Increase estimate" disabled={estimate >= 120} onClick={() => changeEstimate(estimate + 5)}>+</Btn>
                  </div>
                  <p className="mt-2 font-mono text-[12px] text-ink-3">sets the run's time box · cap 90</p>
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
                <Field label="Depends on" optional>
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
                    <input id="new-Depends on" className={`${INPUT} !mt-0`} value={dependency} onChange={event => setDependency(event.target.value)} onKeyDown={event => { if (event.key === "Enter") { event.preventDefault(); addDependency(); } }} />
                    <Btn onClick={addDependency}>Add</Btn>
                  </div>
                </Field>
              </div>
              <div className="space-y-5">
                <label className="flex items-center gap-3 text-[14px] font-semibold">
                  <input type="checkbox" role="switch" checked={uiChange !== null} onChange={toggleUI} className="h-5 w-9 accent-act" />This ticket changes the UI
                </label>
                {uiChange && <>
                  <Field label="Mock-up link" extra=" for a UI change" hint="The design the implementer builds to and the reviewer compares against.">
                    <input id="new-Mock-up link" className={INPUT} placeholder="https://…" value={mockupLink} onChange={event => setMockupLink(event.target.value)} />
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
              <div className="border border-rule bg-paper p-5">
                <Btn variant="primary" busy={busy} busyLabel="Filing…" onClick={() => void file()}>File ticket</Btn>
                <p className="mt-3 font-mono text-[12px] text-ink-2">Files to {project} Backlog.</p>
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
