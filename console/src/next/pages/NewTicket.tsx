import { useContext, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { fileTicket } from "../../lib/boardWrites";
import { formatClock } from "../../lib/format";
import { projectName } from "../../lib/derive";
import { checkSummary, fileNote } from "../checker";
import { Btn } from "../components/Btn";
import { CheckerPanel, checkMarks, CriteriaField, DependsField, EstimateField, Field, INPUT, Messages, messagesId, PriorityField, useTicketCheck } from "../components/TicketFields";
import { useToast } from "../components/Toast";
import { switcherRows } from "../projects";
import { useBoards, useReadiness } from "../queries";
import { PollDepsContext, useConsolePeers } from "../scope";
import { blankForm, clampEstimate, composeBody, fieldOf, parseBody, withField } from "../ticketForm";
import { hostCards } from "../tickets";
import { CRUMB_LINK } from "./runShared";

const FIELDS = ["title", "summary", "what", "why", "how", "inScope", "outOfScope", "verify", "notes"] as const;
type TextField = typeof FIELDS[number];

const emptyValues = () => Object.fromEntries(FIELDS.map(field => [field, ""])) as Record<TextField, string>;
const draftKey = (project: string) => `holo.newTicket.${project}`;

function newForm() {
  const form = blankForm();
  form.sections = form.sections.filter(section => section.name !== "Acceptance criteria" && section.name !== "Estimate & dependencies");
  return withField(form, "estimate", 25);
}

function readDraft(project: string) {
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey(project)) ?? "null");
    if (!draft || typeof draft.body !== "string" || typeof draft.savedAt !== "number" ||
      !Number.isFinite(new Date(draft.savedAt).getTime()) ||
      !(draft.priority === null || (Number.isInteger(draft.priority) && draft.priority >= 0 && draft.priority <= 4))) return null;
    const form = parseBody(draft.body);
    const values = emptyValues();
    for (const field of FIELDS) {
      const value = fieldOf(form, field);
      values[field] = Array.isArray(value) ? value.join("\n") : field === "notes" ? value.replace(/^- /gm, "") : value;
    }
    const proseStart = form.sections.findIndex(section => section.name === "What / Why / How");
    const proseEnd = form.sections.map(section => section.name).lastIndexOf("In scope");
    const proseSections = proseStart < 0 ? [] : form.sections.slice(proseStart, proseEnd < 0 ? undefined : proseEnd);
    const prose = proseSections.map((section, index) => (index ? section.heading : "") + section.text).join("");
    restoreProse(prose, values);
    return { form, values, priority: draft.priority as number | null, savedAt: draft.savedAt as number };
  } catch {
    return null;
  }
}

function restoreProse(prose: string, values: Record<TextField, string>) {
  let fence = "";
  let field: "what" | "why" | "how" | null = null;
  let start = 0;
  const finish = (end: number) => {
    if (field) values[field] = prose.slice(start, end).trimEnd();
  };
  for (const line of prose.matchAll(/[^\n]+|\n/g)) {
    const marker = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line[0]);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length && !marker[2].trim()) fence = "";
      continue;
    }
    if (fence) continue;
    const label = /^\*\*(What|Why|How|UI change|Blast radius):\*\*[ \t]*/.exec(line[0]);
    if (!label) continue;
    finish(line.index);
    field = label[1] === "What" ? "what" : label[1] === "Why" ? "why" : label[1] === "How" ? "how" : null;
    start = line.index + label[0].length;
  }
  finish(prose.length);
}

function clearDraft(project: string) {
  try {
    localStorage.removeItem(draftKey(project));
  } catch {
    return;
  }
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
  const [template, setTemplate] = useState(newForm);
  const [priority, setPriority] = useState<number | null>(2);
  const [dependency, setDependency] = useState("");
  const [mockupLink, setMockupLink] = useState("");
  const [mockupBoards, setMockupBoards] = useState("");
  const [approval, setApproval] = useState(() => `Approved ${new Date().toISOString().slice(0, 10)}: `);
  const [criteria, setCriteria] = useState([""]);
  const [estimateDraft, setEstimateDraft] = useState("25");
  const estimate = clampEstimate(Number(estimateDraft) || 25);
  const dependsOn = fieldOf(template, "dependsOn");
  const uiChange = fieldOf(template, "uiChange");
  const cards = boards.filter(({ host }) => host.base === target?.base).flatMap(({ host, board }) => hostCards(host, board, readiness, peers.now));
  const toggleUI = () => setTemplate(previous => withField(previous, "uiChange", fieldOf(previous, "uiChange") ? null : "major"));
  const [values, setValues] = useState(emptyValues);
  const [restoredAt, setRestoredAt] = useState<number | null>(null);
  const restore = (draft: ReturnType<typeof readDraft>) => {
    const form = draft?.form ?? newForm();
    setTemplate(withField(withField(newForm(), "dependsOn", fieldOf(form, "dependsOn")), "uiChange", fieldOf(form, "uiChange")));
    setValues(draft?.values ?? emptyValues());
    setPriority(draft ? draft.priority : 2);
    setRestoredAt(draft?.savedAt ?? null);
    setDependency("");
    setCriteria(fieldOf(form, "criteria").length ? fieldOf(form, "criteria") : [""]);
    setEstimateDraft(String(fieldOf(form, "estimate") ?? 25));
    setMockupLink(fieldOf(form, "mockupLink"));
    setMockupBoards(fieldOf(form, "mockupImages").map(image => image.replace(/^!\[[^\]]*\]\((.*)\)$/, "$1")).join("\n"));
    setApproval(fieldOf(form, "mockupApproval") || `Approved ${new Date().toISOString().slice(0, 10)}: `);
  };
  useEffect(() => {
    restore(readDraft(project));
  }, [project]);
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
  const checked = checkMarks(check);
  const { judged, messagesOf, marks } = checked;
  const errors = judged?.problems.length ?? 0;
  const held = blank || errors > 0;
  const summary = checkSummary(check);
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
  const save = () => {
    try {
      localStorage.setItem(draftKey(project), JSON.stringify({ body, priority, savedAt: Date.now() }));
      toast("Draft kept in this browser.");
    } catch {
      toast("This browser keeps no drafts.");
    }
  };
  const startOver = () => {
    clearDraft(project);
    restore(null);
    setProblems([]);
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
    clearDraft(project);
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
          {restoredAt !== null && <p className="mt-5 font-mono text-[12.5px] text-ink-2">
            Draft from {formatClock(restoredAt)} restored.{" "}
            <button type="button" disabled={busy} onClick={startOver} className="cursor-pointer underline underline-offset-2 hover:text-ink">Start over</button>
          </p>}
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
              <CriteriaField criteria={criteria} onChange={setCriteria} marks={checked} />
              {input("verify", "Verify commands", "The implementer runs these before review; a red one fails the run.")}
              <div className="flex flex-wrap gap-6">
                <EstimateField draft={estimateDraft} onDraft={setEstimateDraft} estimate={estimate} marks={checked} />
                <PriorityField priority={priority} onChange={setPriority} />
                <DependsField dependsOn={dependsOn} onChange={next => setTemplate(previous => withField(previous, "dependsOn", next))} dependency={dependency} onDependency={setDependency} cards={cards} marks={checked} />
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
              <CheckerPanel summary={summary} marks={checked} />
              <div className="border border-rule bg-paper p-5">
                <div className="flex flex-wrap gap-2">
                  <Btn variant="primary" busy={busy} busyLabel="Filing…" disabled={held} onClick={() => void file()}>File ticket</Btn>
                  <Btn disabled={busy || ((priority === null || priority === 0 || priority === 2) && !FIELDS.some(field => values[field].trim()) && !criteria.some(value => value.trim()) && estimate === 25 && !dependsOn.length && !dependency.trim() && !uiChange)} onClick={save}>Save draft</Btn>
                </div>
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
