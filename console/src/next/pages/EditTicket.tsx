import { useContext, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useQueryClient } from "@tanstack/react-query";
import { editTicket } from "../../lib/boardWrites";
import { formatAge } from "../../lib/format";
import { checkSummary, count } from "../checker";
import { Btn } from "../components/Btn";
import { CHANGED, CheckerPanel, checkMarks, CriteriaField, DependsField, EstimateField, Field, INPUT, Messages, messagesId, PriorityField, useTicketCheck } from "../components/TicketFields";
import { useToast } from "../components/Toast";
import { useBoards, useReadiness } from "../queries";
import { PollDepsContext } from "../scope";
import { useTicketPage } from "../ticket";
import { changedNames, composeEdit, hasMockup, loadEdit, rawParts, same, type EditFields } from "../ticketEdit";
import { clampEstimate } from "../ticketForm";
import { hostCards } from "../tickets";
import { NotFound } from "./Placeholder";
import { CRUMB_LINK } from "./runShared";

const SETTLED = new Set(["merged", "abandoned"]);
const COLUMN_WORDS: Record<string, string> = { ready: "Ready", backlog: "Backlog" };

function Frame({ project, ticketKey, children }: { project: string; ticketKey: string; children?: ReactNode }) {
  return (
    <div className="px-6 pb-8 pt-5 max-[720px]:px-4">
      <section aria-labelledby="page-title" className="mx-auto max-w-[1180px]">
        <nav aria-label="Breadcrumb" className="font-mono text-[12px] text-ink-3">
          <Link to="/tickets" search={{}} className={CRUMB_LINK}>Tickets</Link>{" / "}
          <Link to="/tickets" search={{ project }} className={CRUMB_LINK}>{project}</Link>{" / "}
          <Link to="/tickets/$project/$key" params={{ project, key: ticketKey }} search={{}} className={CRUMB_LINK}>{ticketKey}</Link>{" / edit"}
        </nav>
        <h1 id="page-title" className="mt-5 text-[24px] font-semibold text-ink">Edit {ticketKey}</h1>
        {children}
      </section>
    </div>
  );
}

/** The revision the page loaded: its body and priority, and who saved it when. */
interface Loaded {
  revision: number;
  body: string;
  priority: number | null;
  at: number | null;
  author: string | null;
  column: string | null;
}

/** The note under Save: what it would change, or why it is held. */
function saveNote(changes: string[], errors: number, blocks: boolean, ticketKey: string): string {
  if (!changes.length) return "No changes yet.";
  if (errors && blocks) return `Fix ${count(errors, "error", "errors")} to save.`;
  if (errors) return `${count(errors, "error", "errors")} · it saves as a draft and stays in Backlog.`;
  return `${count(changes.length, "change", "changes")} · ${changes.join(", ")} · the next run of ${ticketKey} reads the new text`;
}

export function EditTicket() {
  const { project, key } = useParams({ from: "/tickets/$project/$key/edit" });
  const page = useTicketPage(project, key);
  const deps = useContext(PollDepsContext);
  const host = page.kind === "ticket" ? [page.host] : [];
  const board = useBoards(host, deps)[0]?.board;
  const readiness = useReadiness(host, deps);
  if (page.kind === "unknown_project") return <NotFound />;
  if (page.kind === "not_mirrored") {
    return <Frame project={project} ticketKey={key}><p className="mt-6 font-mono text-[13px] text-ink-3">No host of {project} has mirrored {key}.</p></Frame>;
  }
  if (page.kind === "loading" || board == null) return <Frame project={project} ticketKey={key}><p className="mt-6 text-ink-3">Loading {key}…</p></Frame>;
  const { ticket } = page;
  const card = board.columns.flatMap((column) => column.tickets).find((each) => each.ticket === key);
  const revision = ticket.current?.revision ?? card?.revision ?? null;
  const settled = SETTLED.has(ticket.status);
  if (board.editable !== true || revision == null || settled) {
    return (
      <Frame project={project} ticketKey={key}>
        <div className="mt-6 font-mono text-[13px] text-ink-2">
          <p>{settled ? `${key} is ${ticket.status} and can no longer be edited.` : `${key} cannot be edited from the console: its board is on Linear, or its host has actions off.`}</p>
          <Link to="/tickets/$project/$key" params={{ project, key }} search={{}} className="mt-3 inline-block text-act underline">Back to {key}</Link>
        </div>
      </Frame>
    );
  }
  const loaded: Loaded = {
    revision,
    body: ticket.current?.body ?? ticket.body,
    priority: ticket.current?.priority ?? card?.priority ?? null,
    at: ticket.current?.at ?? null,
    author: ticket.current?.author ?? null,
    column: ticket.current?.column ?? card?.column ?? null,
  };
  const cards = hostCards(page.host, board, readiness, page.now);
  return <EditForm key={`${project}/${key}`} project={project} ticketKey={key} base={page.host.base} loaded={loaded} cards={cards} now={page.now} />;
}

function EditForm({ project, ticketKey, base, loaded, cards, now }: { project: string; ticketKey: string; base: string; loaded: Loaded; cards: ReturnType<typeof hostCards>; now: number }) {
  const deps = useContext(PollDepsContext);
  const navigate = useNavigate();
  const toast = useToast();
  const client = useQueryClient();
  const [read] = useState(() => ({ ...loaded, fields: loadEdit(loaded.body, loaded.priority), mockup: hasMockup(loaded.body), raw: rawParts(loaded.body) }));
  const drawn = (part: string) => !read.raw.has(part);
  const [fields, setFields] = useState<EditFields>(read.fields);
  const [dependency, setDependency] = useState("");
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string[]>([]);
  const changes = changedNames(read.fields, fields);
  const body = composeEdit(read.body, read.fields, fields);
  const check = useTicketCheck(base, body, deps, ticketKey);
  const checked = checkMarks(check);
  const { judged, messagesOf, marks } = checked;
  const errors = judged?.problems.length ?? 0;
  const blocks = judged?.blocks === true;
  const set = <K extends keyof EditFields>(field: K, value: EditFields[K]) => setFields(previous => ({ ...previous, [field]: value }));
  const changed = (field: keyof EditFields) => !same(read.fields[field], fields[field]);
  const estimate = clampEstimate(Number(fields.estimate) || 25);

  const input = (field: "title" | "summary" | "what" | "why" | "how" | "inScope" | "outOfScope" | "verify" | "notes", label: string, hint?: string, optional = false) => {
    const props = { id: `new-${label}`, value: fields[field], onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => set(field, event.target.value) };
    return <Field label={label} hint={hint} optional={optional} messages={messagesOf(label)} changed={changed(field)} extra={field === "verify" ? " · one a line, run as written" : undefined}>
      {field === "title" ? <input {...props} {...marks(label, `${INPUT} font-sans text-[18px] font-semibold`)} /> :
        <textarea {...props} rows={field === "summary" || field === "what" || field === "why" ? 2 : 3} {...marks(label, `${INPUT} resize-y ${field === "verify" ? "font-mono" : "font-sans"}`)} />}
    </Field>;
  };
  const discard = () => {
    setFields(read.fields);
    setDependency("");
    setRefusal([]);
  };
  const save = async () => {
    if (busy || !changes.length || blocks) return;
    setBusy(true);
    setRefusal([]);
    const result = await editTicket(base, ticketKey, read.revision, body, deps.fetch, fields.priority);
    setBusy(false);
    if (!result.ok) {
      setRefusal("conflict" in result ? [`${ticketKey} changed since this page read it. Your edit is kept.`] : "problems" in result ? result.problems : [result.error]);
      return;
    }
    toast(`Saved ${ticketKey} as revision ${result.revision}.`);
    void client.invalidateQueries({ queryKey: ["fleet"] });
    void client.invalidateQueries({ queryKey: ["route", base] });
    void navigate({ to: "/tickets/$project/$key", params: { project, key: ticketKey }, search: {} });
  };
  const line = [`revision ${read.revision}`, read.at != null && `saved ${formatAge(now - read.at)} ago${read.author ? ` by ${read.author}` : ""}`, read.column != null && COLUMN_WORDS[read.column]].filter(Boolean).join(" · ");

  return (
    <Frame project={project} ticketKey={ticketKey}>
      <p className="mt-1.5 border-b border-rule pb-[18px] font-mono text-[13px] text-ink-2">{line}</p>
      <div className="mt-6 grid min-w-0 items-start gap-6 min-[1100px]:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 divide-y divide-rule-2 border border-rule bg-paper [&>div]:p-5">
          <Field label="Project">
            <select id="new-Project" className={`${INPUT} font-sans`} value={project} disabled><option value={project}>{project}</option></select>
          </Field>
          {drawn("Title") && input("title", "Title", "The outcome, as the board card says it.")}
          {drawn("Summary") && input("summary", "Summary", "One or two sentences. Shown under the title on the card and in peek.")}
          {drawn("What / Why / How") && <div className="grid gap-5 min-[720px]:grid-cols-2">
            {input("what", "What")}{input("why", "Why", undefined, true)}
          </div>}
          {drawn("What / Why / How") && input("how", "How")}
          {drawn("In scope") && input("inScope", "In scope", "One a line.")}
          {drawn("Out of scope") && input("outOfScope", "Out of scope", "One a line.")}
          {drawn("Acceptance criteria") && <CriteriaField criteria={fields.criteria} marks={checked}
            onChange={(next, from) => setFields(previous => ({
              ...previous,
              criteria: next,
              criteriaFrom: from.map(index => index === null ? null : previous.criteriaFrom[index] ?? null),
              criteriaDetail: from.map(index => index === null ? "" : previous.criteriaDetail[index] ?? ""),
            }))}
            details={fields.criteriaDetail.map((detail, index) => {
              const from = fields.criteriaFrom[index];
              return detail || (from != null && read.fields.criteriaDetail[from]) ? detail : null;
            })}
            onDetail={(index, detail) => set("criteriaDetail", fields.criteriaDetail.map((each, at) => at === index ? detail : each))}
            changed={index => fields.criteriaFrom[index] !== index || fields.criteria[index] !== read.fields.criteria[index]}
            detailChanged={index => {
              const from = fields.criteriaFrom[index];
              return fields.criteriaDetail[index] !== (from == null ? "" : read.fields.criteriaDetail[from]);
            }} />}
          {drawn("Verify command(s)") && input("verify", "Verify commands", "The implementer runs these before review; a red one fails the run.")}
          <div className="flex flex-wrap gap-6">
            {drawn("Estimate & dependencies") && <EstimateField draft={fields.estimate} onDraft={draft => set("estimate", draft)} estimate={estimate} marks={checked} changed={changed("estimate")} />}
            <PriorityField priority={fields.priority} onChange={value => set("priority", value)} changed={changed("priority")} />
            {drawn("Estimate & dependencies") && <DependsField dependsOn={fields.dependsOn} onChange={next => set("dependsOn", next)} dependency={dependency} onDependency={setDependency} cards={cards} marks={checked} changed={changed("dependsOn")} />}
          </div>
          {drawn("What / Why / How") && <div className="space-y-5">
            <label data-changed={changed("uiChange") || undefined} className={`flex items-center gap-3 text-[14px] font-semibold ${changed("uiChange") ? `${CHANGED} py-2 pl-2` : ""}`}>
              <input id="new-UI change" type="checkbox" role="switch" checked={fields.uiChange !== null} onChange={() => set("uiChange", fields.uiChange ? null : read.fields.uiChange ?? "major")} {...marks("UI change", "h-5 w-9 accent-act")} />This ticket changes the UI
            </label>
            <Messages id={messagesId("UI change")} messages={messagesOf("UI change")} />
            {(fields.uiChange || read.mockup) && drawn("Mock-up") && <>
              <Field label="Mock-up link" extra=" for a UI change" messages={messagesOf("Mock-up link")} changed={changed("mockupLink")}>
                <input id="new-Mock-up link" {...marks("Mock-up link", INPUT)} placeholder="https://…" value={fields.mockupLink} onChange={event => set("mockupLink", event.target.value)} />
              </Field>
              <Field label="Approval" changed={changed("mockupApproval")}>
                <input id="new-Approval" className={`${INPUT} font-mono`} value={fields.mockupApproval} onChange={event => set("mockupApproval", event.target.value)} />
              </Field>
              <Field label="Boards" optional hint="One image link a line." changed={changed("mockupImages")}>
                <textarea id="new-Boards" rows={3} className={`${INPUT} font-mono`} value={fields.mockupImages} onChange={event => set("mockupImages", event.target.value)} />
              </Field>
            </>}
          </div>}
          {drawn("Implementation notes") && input("notes", "Implementation notes", "Files, helpers and constraints the implementer should know.", true)}
          {fields.others.length > 0 && <div>
            <details open={read.raw.size > 0}>
              <summary className="cursor-pointer font-mono text-[11px] font-medium uppercase tracking-[.12em] text-ink-2">
                Other sections <span className="normal-case tracking-[.04em] text-ink-3">{fields.others.map(other => other.name).join(" · ")}</span>
              </summary>
              <div className="mt-4 space-y-5">
                {fields.others.map((other, at) => <Field key={other.index} label={other.name} optional changed={other.text !== read.fields.others[at]?.text}>
                  <textarea id={`new-${other.name}`} rows={3} className={`${INPUT} resize-y font-sans`} value={other.text}
                    onChange={event => set("others", fields.others.map((each, index) => index === at ? { ...each, text: event.target.value } : each))} />
                </Field>)}
              </div>
            </details>
          </div>}
        </div>
        <aside aria-label="Save revision" className="min-w-0">
          <CheckerPanel summary={checkSummary(check, true)} marks={checked} />
          <div className="border border-rule bg-paper p-5">
            <div className="flex flex-wrap gap-2">
              <Btn variant="primary" busy={busy} busyLabel="Saving…" disabled={!changes.length || blocks} onClick={() => void save()}>Save revision {read.revision + 1}</Btn>
              <Btn disabled={busy || !changes.length} onClick={discard}>Discard</Btn>
            </div>
            <p className="mt-3 font-mono text-[12px] text-ink-2">{saveNote(changes, errors, blocks, ticketKey)}</p>
            {refusal.length > 0 && <ul role="alert" className="mt-4 space-y-2 text-[13px] text-bad [overflow-wrap:anywhere]">{refusal.map((problem, index) => <li key={index}>{problem}</li>)}</ul>}
          </div>
          <p className="mt-4 font-mono text-[12px] text-ink-3">Saved as the next revision. Errors block a Ready ticket; a Backlog ticket saves as a draft.</p>
        </aside>
      </div>
    </Frame>
  );
}
