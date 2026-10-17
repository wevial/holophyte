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
import { blankForm, composeBody, withField } from "../ticketForm";
import { CRUMB_LINK } from "./runShared";

const INPUT = "mt-2 w-full min-w-0 rounded border border-rule bg-canvas px-3 py-2 font-sans text-[14px] text-ink placeholder:text-ink-3 focus:outline-act";
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
  const rows = switcherRows(peers.hosts, peers.now, new Map());
  const editable = rows.flatMap(row => {
    const match = boards.find(({ host, board }) => board.editable && host.project != null && projectName(host.project) === row.name);
    return match && row.name != null ? [{ name: row.name, base: match.host.base }] : [];
  });
  const target = editable.find(row => row.name === project);
  const answered = boards.some(({ host }) => host.project != null && projectName(host.project) === project);
  const [template] = useState(blankForm);
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
      {field === "title" ? <input {...props} className={`${INPUT} text-[18px] font-semibold`} /> :
        <textarea {...props} rows={field === "summary" || field === "what" || field === "why" ? 2 : 3} className={`${INPUT} resize-y ${field === "verify" ? "font-mono" : ""}`} />}
    </Field>;
  };
  const file = async () => {
    if (!target || busy) return;
    setBusy(true);
    setProblems([]);
    let form = template;
    for (const field of FIELDS) {
      if (field === "inScope" || field === "outOfScope" || field === "verify") form = withField(form, field, values[field] ? values[field].split("\n") : []);
      else form = withField(form, field, values[field]);
    }
    const result = await fileTicket(target.base, composeBody(form), null, deps.fetch, "backlog");
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
                <select id="new-Project" className={INPUT} value={project} disabled={busy} onChange={event => {
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
              {input("verify", "Verify commands", "The implementer runs these before review; a red one fails the run.")}
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
