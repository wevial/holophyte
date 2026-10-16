import { useState, type ReactNode } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { criteriaRows } from "../../lib/criteria";
import { formatAge, formatClock } from "../../lib/format";
import { summaryOf } from "../../lib/ticket";
import { classicHref } from "../../lib/view";
import { Btn } from "../components/Btn";
import { Criteria, SECTION_LABEL } from "../components/Criteria";
import { StateDot } from "../components/StateDot";
import { SteerBox } from "../components/SteerBox";
import { SteerNotes } from "../components/SteerNotes";
import { StoryPanel } from "../components/StoryPanel";
import { Chip, Fact, NUMBER } from "../components/TicketPeek";
import { TicketRuns } from "../components/TicketRuns";
import { useTicketPage, type BoardChip } from "../ticket";
import { NotFound } from "./Placeholder";

const CRUMB_LINK = "border-b border-rule text-ink-3 no-underline hover:border-ink hover:text-ink";

function Page({ project, ticketKey, children }: { project: string; ticketKey: string; children?: ReactNode }) {
  return (
    <div className="px-6 pb-8 pt-5 max-[720px]:px-4">
      <section aria-label={ticketKey} className="mx-auto max-w-[1180px]">
        <nav aria-label="Breadcrumb" className="font-mono text-[12px] leading-none text-ink-3">
          <Link to="/tickets" search={{}} className={CRUMB_LINK}>Tickets</Link>
          {" / "}
          <Link to="/tickets" search={{ project }} className={CRUMB_LINK}>{project} tickets</Link>
          {" / "}
          <span>{ticketKey}</span>
        </nav>
        {children}
      </section>
    </div>
  );
}

function BoardLine({ chip, liveRun, base }: { chip: BoardChip; liveRun: number | null; base: string }) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2.5">
      <span className="font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">Board</span>
      <span role="status" aria-label="Board">
        <Chip>
          <StateDot state={chip.state} hideWord tone={chip.need ? "need" : undefined} />
          <span>
            {chip.label}
            {chip.sub != null && <span className="font-normal text-ink-3"> · {chip.sub}</span>}
          </span>
        </Chip>
      </span>
      {liveRun != null && (
        <a href={classicHref(`#run=${liveRun}&daemon=${encodeURIComponent(base)}`)} className="font-mono text-[12px] leading-[1.3] text-ink-2 underline underline-offset-2">
          open run {liveRun}
        </a>
      )}
    </div>
  );
}

function filedAt(at: number, now: number): string {
  return new Date(at).toDateString() === new Date(now).toDateString() ? formatClock(at) : new Date(at).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

const SETTLED = new Set(["merged", "abandoned"]);

export function TicketPage() {
  const { project, key } = useParams({ from: "/tickets/$project/$key" });
  const page = useTicketPage(project, key);
  const [steering, setSteering] = useState(false);
  if (page.kind === "unknown_project") return <NotFound />;
  if (page.kind === "loading") return <Page project={project} ticketKey={key} />;
  if (page.kind === "not_mirrored") {
    return (
      <Page project={project} ticketKey={key}>
        <p className="mt-6 font-mono text-[13px] text-ink-3">
          No host of {project} has mirrored {key}.
        </p>
        <Link to="/tickets" search={{ project }} className="mt-3 inline-block font-sans text-[13px] font-medium text-act underline underline-offset-2">
          Back to Tickets
        </Link>
      </Page>
    );
  }
  const { ticket, chip, labels, judged, now } = page;
  const commands = ticket.verification_commands;
  const first = [...ticket.revisions].sort((a, b) => a.revision - b.revision)[0];
  const waitsOn = page.card?.waitsOn ?? [];
  const actionsOn = page.host.status?.actions === true;
  return (
    <Page project={project} ticketKey={key}>
      <div className="border-b border-rule pb-3">
        <div className="mt-2 flex flex-wrap items-start justify-between gap-x-6 gap-y-2.5">
          <h1 className="m-0 flex-[1_1_420px] font-sans text-[20px] font-semibold leading-[1.25] tracking-[-.005em] text-ink">
            {ticket.title}
          </h1>
          <div data-ticket-actions className="flex flex-wrap gap-2">
            {!SETTLED.has(ticket.status) && (
              <Btn variant="primary" expanded={steering && actionsOn} disabled={!actionsOn} title={actionsOn ? undefined : ACTIONS_OFF} onClick={() => setSteering((open) => !open)}>
                Steer
              </Btn>
            )}
          </div>
        </div>
        {chip != null && <BoardLine chip={chip} liveRun={page.liveRun} base={page.host.base} />}
      </div>
      {steering && actionsOn && !SETTLED.has(ticket.status) && (
        <SteerBox ticketKey={key} base={page.host.base} liveRun={page.liveRun} onClose={() => setSteering(false)} />
      )}
      <div className="mt-3.5 grid border border-rule bg-paper min-[860px]:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 border-b border-rule px-8 pb-8 pt-[22px] max-[720px]:px-5 min-[860px]:border-b-0 min-[860px]:border-r">
          <div className={SECTION_LABEL}>Summary</div>
          <p className="m-0 mt-3 max-w-[70ch] font-sans text-[14px] leading-[1.5] text-ink">{summaryOf(ticket.body)}</p>
          <div className="mt-[26px]">
            <Criteria
              rows={criteriaRows(ticket.acceptance_criteria, typeof judged === "string" ? [] : judged, ticket.run != null)}
              keys
              run={ticket.run}
              held={typeof judged === "string" ? judged : null}
            />
          </div>
          <details className="mt-2.5">
            <summary className="flex min-h-11 cursor-pointer items-center gap-2.5 font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-2">
              Verify
              <span className="normal-case tracking-[.04em] text-ink-3">
                {commands.length} {commands.length === 1 ? "command" : "commands"}
              </span>
            </summary>
            <pre className="m-0 whitespace-pre-wrap border border-rule bg-paper-2 px-3.5 py-3 font-mono text-[12.5px] leading-[1.7] [overflow-wrap:anywhere] text-ink">
              {commands.join("\n")}
            </pre>
          </details>
          <TicketRuns ticketKey={key} runs={ticket.runs} liveRuns={page.liveRuns} staleMs={page.staleMs} base={page.host.base} now={now} />
          <SteerNotes steers={ticket.steers} now={now} />
        </div>
        <aside aria-label="Ticket facts and story" className="min-w-0 px-6 pb-7 pt-[22px] max-[720px]:px-5">
          <div className={SECTION_LABEL}>Ticket</div>
          <dl className="mt-3 grid grid-cols-[96px_minmax(0,1fr)] items-baseline gap-x-3 gap-y-2.5">
            <Fact label="Project">
              <Link to="/tickets" search={{ project }} className="text-ink underline-offset-2 hover:underline">{project}</Link>
            </Fact>
            <Fact label="Time box">{ticket.time_box_ms == null ? "none" : <span className={NUMBER}>{formatAge(ticket.time_box_ms)}</span>}</Fact>
            {labels != null && <Fact label="Implementer">{labels.implementer}</Fact>}
            {labels != null && <Fact label="Reviewer">{labels.reviewer}</Fact>}
            {first != null && (
              <Fact label="Filed">
                <span className={NUMBER}>{filedAt(first.at, now)}</span> by {first.author}
              </Fact>
            )}
            {waitsOn.length > 0 && <Fact label="Waits on">{waitsOn.join(", ")}</Fact>}
          </dl>
          {page.story != null && <StoryPanel ticketKey={key} project={project} entry={page.story.entry} detail={page.story.detail} />}
        </aside>
      </div>
    </Page>
  );
}
