import { useState, type ReactNode } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { ACTIONS_OFF } from "../../lib/actions";
import { criteriaRows } from "../../lib/criteria";
import { formatAge, formatClock } from "../../lib/format";
import { summaryOf } from "../../lib/ticket";
import { Btn } from "../components/Btn";
import { MoreMenu, type MenuItem } from "../components/MoreMenu";
import { usePlaceItems, useRevertFlow } from "../components/Revert";
import { Criteria, SECTION_LABEL } from "../components/Criteria";
import { StateDot, TONE_TEXT } from "../components/StateDot";
import { ScoutThis, useScoutThis } from "../components/ScoutThis";
import type { ScoutTarget } from "../scouts";
import { SteerBox } from "../components/SteerBox";
import { SteerNotes } from "../components/SteerNotes";
import { StoryPanel } from "../components/StoryPanel";
import { Fact, NUMBER } from "../components/TicketPeek";
import { TicketBody, type BodyView } from "../components/TicketBody";
import { TicketMoves } from "../components/TicketMoves";
import { TicketRuns } from "../components/TicketRuns";
import { TitleText } from "../components/TitleText";
import { runPage } from "../run";
import { STATES } from "../state";
import { useTicketPage, type BoardChip } from "../ticket";
import type { TicketDetail } from "../../lib/contract.gen";
import { NotFound } from "./Placeholder";
import { CRUMB_LINK } from "./runShared";

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

function BoardLine({ chip, liveRun, project }: { chip: BoardChip; liveRun: number | null; project: string }) {
  const tone = chip.need ? "need" : STATES[chip.state].tone;
  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-x-3.5 gap-y-2.5">
      <span role="status" aria-label="Board" className="inline-flex items-center gap-1.5 font-sans text-[13px] font-medium leading-[1.3]">
        <StateDot state={chip.state} hideWord tone={tone} />
        <span>
          <span className={TONE_TEXT[tone]}>{chip.label}</span>
          {chip.sub != null && <span className="font-normal text-ink-3"> · {chip.sub}</span>}
        </span>
      </span>
      {liveRun != null && (
        <Link {...runPage(project, liveRun)} className="font-sans text-[13px] leading-[1.3] text-ink-3 no-underline hover:text-ink hover:underline underline-offset-2">
          open run {liveRun}
        </Link>
      )}
    </div>
  );
}

function filedAt(at: number, now: number): string {
  return new Date(at).toDateString() === new Date(now).toDateString() ? formatClock(at) : new Date(at).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

const SETTLED = new Set(["merged", "abandoned"]);

const scoutOf = (project: string, key: string, actions: boolean): ScoutTarget => ({ project, about: `ticket:${key}`, kind: "research", ask: `${key}: `, off: actions ? null : ACTIONS_OFF });

/** The header's ⋯ menu: Steer while unsettled, Scout, the ticket's
 *  `moves`, Copy link, and Revert… when the ticket's latest run merged in a project that reports
 *  `reverts`. */
function TicketMenu({ project, ticketKey, ticket, base, reverts, actions, onSteer, moves }: {
  project: string; ticketKey: string; ticket: TicketDetail; base: string; reverts: boolean; actions: boolean; onSteer: () => void; moves: MenuItem[];
}) {
  const latest = ticket.runs[0];
  const off = actions ? undefined : ACTIONS_OFF;
  const scout = useScoutThis(scoutOf(project, ticketKey, actions));
  const pageItems: MenuItem[] = [
    ...(SETTLED.has(ticket.status) ? [] : [{ label: "Steer", onSelect: onSteer, disabled: !actions, note: off }]),
    { label: "Scout", onSelect: scout, disabled: !actions, note: off },
    ...moves,
  ];
  const revertable = reverts && latest?.outcome === "merged";
  const revert = useRevertFlow(revertable ? { base, project, ticket: ticketKey, run: latest.run, newest: true, reverted: ticket.reverted, off: actions ? null : ACTIONS_OFF } : null, [...pageItems, ...usePlaceItems({ project, ticket: ticketKey })]);
  return (
    <div className="flex flex-col items-end gap-2">
      <MoreMenu label={`More for ${ticketKey}`} items={revert.items} />
      {revert.layer}
    </div>
  );
}

export function TicketPage() {
  const { project, key } = useParams({ from: "/tickets/$project/$key" });
  const page = useTicketPage(project, key);
  const [steeringKey, setSteeringKey] = useState<string | null>(null);
  const [body, setBody] = useState<{ at: string; view: BodyView; editing: number | null } | null>(null);
  if (body != null && body.at !== `${project}/${key}`) setBody(null);
  if (page.kind === "unknown_project") return <NotFound />;
  if (page.kind === "loading") return <Page project={project} ticketKey={key} />;
  if (page.kind === "not_mirrored") {
    return (
      <Page project={project} ticketKey={key}>
        <p className="m-0 mt-6 font-sans text-[13px] text-ink-3">
          <span>No host of {project} has mirrored {key}.</span>{" "}
          <Link to="/tickets" search={{ project }} className="font-medium text-ink-2 underline underline-offset-2 hover:text-ink">
            Back to Tickets
          </Link>
        </p>
      </Page>
    );
  }
  const { ticket, chip, labels, judged, now } = page;
  const commands = ticket.verification_commands;
  const first = [...ticket.revisions].sort((a, b) => a.revision - b.revision)[0];
  const waitsOn = page.card?.waitsOn ?? [];
  const actionsOn = page.host.status?.actions === true;
  const steering = steeringKey === `${project}/${key}`;
  const { view, editing } = body?.at === `${project}/${key}` ? body : { view: "fields" as const, editing: null };
  const showBody = (next: { view: BodyView; editing: number | null }) => setBody({ at: `${project}/${key}`, ...next });
  const toggleSteer = () => setSteeringKey(steering ? null : `${project}/${key}`);
  const moveProps = {
    project,
    ticketKey: key,
    ticket,
    host: page.host,
    liveRun: page.liveRun,
    onEditRaw: view === "markdown" ? (revision: number) => showBody({ view, editing: revision }) : undefined,
    editOpen: editing != null,
  };
  return (
    <Page project={project} ticketKey={key}>
      <div>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-x-6 gap-y-2.5">
          <h1 className="m-0 flex-[1_1_420px] font-sans text-[20px] font-semibold leading-[1.25] tracking-[-.005em] text-ink">
            <TitleText>{ticket.title}</TitleText>
          </h1>
          <div className="flex items-start gap-2">
            <div data-ticket-actions className="flex flex-wrap gap-2">
              {!SETTLED.has(ticket.status) && (
                <Btn variant="primary" expanded={steering && actionsOn} disabled={!actionsOn} title={actionsOn ? undefined : ACTIONS_OFF} onClick={toggleSteer}>
                  Steer
                </Btn>
              )}
              <ScoutThis target={scoutOf(project, key, actionsOn)} />
              <TicketMoves key={`${project}/${key}`} {...moveProps} />
            </div>
            <div className="flex flex-col items-end gap-2">
              <TicketMoves
                key={`menu ${project}/${key}`}
                {...moveProps}
                menu={(moves) => (
                  <TicketMenu
                    project={project}
                    ticketKey={key}
                    ticket={ticket}
                    base={page.host.base}
                    reverts={page.host.status?.reverts === true}
                    actions={actionsOn}
                    onSteer={toggleSteer}
                    moves={moves}
                  />
                )}
              />
            </div>
          </div>
        </div>
        {chip != null && <BoardLine chip={chip} liveRun={page.liveRun} project={project} />}
      </div>
      {steering && actionsOn && !SETTLED.has(ticket.status) && (
        <SteerBox key={`${project}/${key}`} ticketKey={key} base={page.host.base} liveRun={page.liveRun} onClose={() => setSteeringKey(null)} />
      )}
      <div data-ticket-card className="mt-5 grid rounded-[10px] bg-raised shadow-[inset_0_0_0_1px_var(--hair)] min-[860px]:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 border-b border-hair px-8 pb-8 pt-[22px] max-[720px]:px-5 min-[860px]:border-b-0 min-[860px]:border-r">
          <TicketBody
            ticketKey={key}
            base={page.host.base}
            text={ticket.current?.body ?? ticket.body}
            view={view}
            editing={editing}
            onView={(next) => showBody({ view: next, editing: null })}
            onClose={() => showBody({ view: "markdown", editing: null })}
          >
            <div className={SECTION_LABEL}>Summary</div>
            <p className="m-0 mt-3 max-w-[75ch] font-sans text-[14px] leading-[1.5] text-ink">{summaryOf(ticket.body)}</p>
            <div className="mt-[26px]">
              <Criteria
                rows={criteriaRows(ticket.criteria, typeof judged === "string" ? [] : judged, ticket.run != null)}
                run={ticket.run}
                held={typeof judged === "string" ? judged : null}
              />
            </div>
            <details className="mt-2.5">
              <summary className="mb-3 flex h-[30px] cursor-pointer items-center gap-2.5 border-b border-hair font-sans text-[13px] font-semibold leading-none text-ink">
                Verify
                <span className="font-mono text-[12px] font-normal text-ink-3">
                  {commands.length} {commands.length === 1 ? "command" : "commands"}
                </span>
              </summary>
              <pre className="m-0 whitespace-pre-wrap rounded-[6px] bg-selected px-3.5 py-3 font-mono text-[12.5px] leading-[1.7] [overflow-wrap:anywhere] text-ink">
                {commands.join("\n")}
              </pre>
            </details>
          </TicketBody>
          <TicketRuns project={project} ticketKey={key} runs={ticket.runs} liveRuns={page.liveRuns} staleMs={page.staleMs} now={now} />
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
