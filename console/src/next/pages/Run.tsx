import { useContext, useRef, type MouseEvent, type ReactNode } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { Markdown } from "../../components/Markdown";
import { TicketLink } from "../../components/TicketLink";
import { formatClock, formatSettled } from "../../lib/format";
import { RunActions } from "../components/RunActions";
import { leadParagraph } from "../../lib/ticket";
import { AgentCompose } from "../components/AgentCompose";
import { AgentPanel } from "../components/AgentPanel";
import { CriteriaList } from "../components/CriteriaList";
import { SECTION_LABEL } from "../components/Criteria";
import { ProgressLog } from "../components/ProgressLog";
import { RunDetails } from "../components/RunDetails";
import { RunPullRequest } from "../components/RunPullRequest";
import { StageChip } from "../components/StageChip";
import { StateDot } from "../components/StateDot";
import { useTicket } from "../queries";
import { useRunPage } from "../run";
import { PollDepsContext } from "../scope";

const CRUMB_LINK = "border-b border-rule text-ink-3 no-underline hover:border-ink hover:text-ink";
const STRONG = "font-medium text-ink-2";

function zoneName(at: number): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(at);
  return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
}

function Page({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="px-6 pb-8 pt-5 max-[720px]:px-4">
      <section aria-label={label} className="mx-auto max-w-[1180px]">
        {children}
      </section>
    </div>
  );
}

function Notice({ label, text }: { label: string; text: string }) {
  return (
    <Page label={label}>
      <p className="m-0 font-mono text-[13px] text-ink-3">{text}</p>
      <Link to="/runs" search={{}} className="mt-3 inline-block font-sans text-[13px] font-medium text-act underline underline-offset-2">
        Back to Runs
      </Link>
    </Page>
  );
}

/** The console routes on the hash, so the in-page link scrolls instead of navigating. */
function toProgress(event: MouseEvent<HTMLAnchorElement>) {
  event.preventDefault();
  document.getElementById("progress")?.scrollIntoView({ block: "start" });
}

/** The summary the run was given: the claimed revision's body when the
 *  ticket answers one, else its current one; nothing on a 404. */
function RunTicket({ base, ticket }: { base: string; ticket: string }) {
  const deps = useContext(PollDepsContext);
  const { data } = useTicket(base, ticket, deps);
  if (data == null) return null;
  const claimed = data.claimed?.revision != null ? data.claimed.body : null;
  const lead = leadParagraph(claimed ?? data.current?.body ?? data.body);
  if (!lead) return null;
  return (
    <section aria-label="Ticket" data-run-ticket>
      <div className={SECTION_LABEL}>Ticket</div>
      <div className="mt-2.5 max-w-[68ch] font-plex text-[15px] leading-[1.55] text-ink-2 [&_p]:m-0">
        <Markdown>{lead}</Markdown>
      </div>
    </section>
  );
}

function Dot() {
  return <>{" · "}</>;
}

export function RunPage() {
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const { project, run: param } = useParams({ from: "/runs/$project/$run" });
  const id = /^\d+$/.test(param) ? Number(param) : null;
  const page = useRunPage(project, id);
  const shown = id == null ? param : String(id);
  const label = `run ${shown}`;
  if (page.kind === "unknown_project") return <Notice label={label} text={`No project named ${project} answers this console.`} />;
  if (page.kind === "not_found") return <Notice label={label} text={`${project} has no run ${shown}.`} />;
  if (page.kind === "loading") {
    return (
      <Page label={label}>
        <p role="status" className="m-0 font-mono text-[13px] text-ink-3">
          Asking {project} for run {shown}…
        </p>
      </Page>
    );
  }
  const { run } = page.detail;
  const box = page.agentMs == null ? null : (
    <>
      <span className={STRONG}>{formatSettled(page.agentMs)}</span>
      {page.boxMs != null && ` of a ${formatSettled(page.boxMs)} box`}
    </>
  );
  return (
    <Page label={label}>
      <header className="border-b border-rule pb-3">
        <nav aria-label="Breadcrumb" className="font-mono text-[12px] leading-none text-ink-3">
          <Link to="/runs" search={{}} className={CRUMB_LINK}>Runs</Link>
          {" / "}
          <Link to="/tickets" search={{ project }} className={CRUMB_LINK}>{project} tickets</Link>
          {" / "}
          <TicketLink ticket={run.ticket} ticket_url={run.ticket_url} />
          {" / "}
          <span>run {run.id}</span>
        </nav>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <h1 className="m-0 font-sans text-[20px] font-semibold leading-[1.25] tracking-[-.005em] text-ink">{run.title}</h1>
          <RunActions key={`${page.base}/${run.id}`} page={page} project={project} />
        </div>
        <p data-run-facts className="m-0 mt-3 font-mono text-[12px] leading-[1.7] text-ink-3 [overflow-wrap:anywhere]">
          <span className={STRONG}>{run.ticket}</span>
          <Dot />
          run {run.id}
          <Dot />
          {run.host == null ? project : `${project} on ${run.host}`}
          <Dot />
          started {formatClock(run.started_ms)} {zoneName(run.started_ms)}
          {box != null && (
            <>
              <Dot />
              {box}
            </>
          )}
          {page.labels != null && (
            <>
              <Dot />
              implementer {page.labels.implementer}
              <Dot />
              reviewer {page.labels.reviewer}
            </>
          )}
        </p>
        <div className="mt-[18px] flex items-start gap-3.5">
          <span className="mt-1">
            <StateDot state={page.state} hideWord />
          </span>
          <p role="status" className="m-0 max-w-[64ch] font-sans text-[17px] leading-[1.45] text-ink">
            {page.sentence}
          </p>
        </div>
        <div className="mt-[18px] flex flex-wrap items-center gap-x-3 gap-y-2.5">
          {page.state !== "merged" && <a href="#agent" className="btn" data-variant="quiet" onClick={(event) => {
            event.preventDefault();
            composeRef.current?.focus();
            composeRef.current?.scrollIntoView({ block: "nearest" });
          }}>Steer or ask</a>}
          <span className="font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">Stage</span>
          <span role="group" aria-label="Stage">
            <StageChip label={page.stage.label} state={page.state} sub={page.stage.sub} />
          </span>
          <a href="#progress" onClick={toProgress} className="font-mono text-[12px] leading-[1.3] text-ink-2 underline underline-offset-2 hover:text-ink">
            history in Progress
          </a>
        </div>
      </header>
      <div data-run-body className="@container mt-3.5 flex min-h-40 flex-wrap border border-rule bg-paper">
        <div className="flex min-w-0 flex-[999_1_560px] flex-col gap-5 px-8 pb-8 pt-[22px] @[880px]:border-r @[880px]:border-rule max-[720px]:px-4">
          {run.pr_url != null && (
            <RunPullRequest
              url={run.pr_url}
              facts={page.prFacts}
              ready={page.state === "ready_to_merge"}
              merge={page.state === "merged" ? run : null}
            />
          )}
          <RunTicket base={page.base} ticket={run.ticket} />
          <CriteriaList detail={page.detail} live={["running", "stale", "question"].includes(page.state)} />
          <ProgressLog key={run.id} lines={page.progress} live={page.live} />
          <RunDetails key={`${page.base}/${run.id}`} base={page.base} detail={page.detail} live={page.live} />
        </div>
        <AgentPanel label={page.labels?.implementer ?? "—"} state={page.agentState} notes={page.detail.steer_notes} asks={page.asks}>
          {page.state !== "merged" && <AgentCompose key={`${page.base}/${run.id}`} base={page.base} run={run.id} ticket={run.ticket} prUrl={run.pr_url} actions={page.actions} parked={page.phase === "awaiting_merge_approval"} live={page.stoppable} ref={composeRef} />}
        </AgentPanel>
      </div>
    </Page>
  );
}
