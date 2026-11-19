import { useContext, useState } from "react";
import { Link, useParams } from "@tanstack/react-router";
import { Markdown } from "../components/Markdown";
import { TicketLink } from "../components/TicketLink";
import { RunActions } from "../components/RunActions";
import { MoreMenu, type MenuItem } from "../components/MoreMenu";
import { usePlaceItems, useRevertFlow } from "../components/Revert";
import { leadParagraph } from "../../lib/ticket";
import { AgentCompose } from "../components/AgentCompose";
import { AgentPanel } from "../components/AgentPanel";
import { CriteriaList } from "../components/CriteriaList";
import { RawLog } from "../components/RawLog";
import { RunActivity } from "../components/RunActivity";
import { RunDetails } from "../components/RunDetails";
import { ACTIONS_OFF_LINE } from "../components/CardActions";
import { RunFacts, TimeBox } from "../components/RunFacts";
import { FoldChevron } from "../components/icons";
import { ScoutThis, useScoutThis } from "../components/ScoutThis";
import type { ScoutTarget } from "../scouts";
import { RunStepper } from "../components/RunStepper";
import { prNumber } from "../components/RunTable";
import { RevertedGlyph, StateDot } from "../components/StateDot";
import { TitleText } from "../components/TitleText";
import { useRunTurns, useTicket } from "../queries";
import { STAGE_NAMES, stepperOf, useRunPage, type RunPage as RunPageFacts } from "../run";
import { keepScope, PollDepsContext } from "../scope";
import type { Reverted, RunDetail } from "../../lib/contract.gen";
import { formatSettled } from "../../lib/format";
import { commitLink, shortSha } from "../../lib/shipped";
import { CRUMB_LINK, Loading, Notice, Page } from "./runShared";

/** The summary the run was given: the claimed revision's body when the
 *  ticket answers one, else its current one; nothing on a 404. */
function RunTicket({ base, ticket }: { base: string; ticket: string }) {
  const deps = useContext(PollDepsContext);
  const { data } = useTicket(base, ticket, deps);
  const [open, setOpen] = useState(false);
  if (data == null) return null;
  const claimed = data.claimed?.revision != null ? data.claimed.body : null;
  const lead = leadParagraph(claimed ?? data.current?.body ?? data.body);
  if (!lead) return null;
  return (
    <details data-run-ticket onToggle={(event) => setOpen(event.currentTarget.open)} className="border-t border-rule pt-1.5">
      <summary className="flex min-h-[34px] cursor-pointer list-none items-center gap-2 font-sans text-[13px] font-semibold text-ink [&::-webkit-details-marker]:hidden">
        <FoldChevron open={open} />
        Ticket
      </summary>
      <div className="max-w-[68ch] pb-1.5 pl-[22px] pt-0.5 font-read text-[15px] leading-[1.55] text-ink-2 [&_p]:m-0">
        <Markdown>{lead}</Markdown>
      </div>
    </details>
  );
}

function Stepper({ page }: { page: Extract<RunPageFacts, { kind: "run" }> }) {
  const deps = useContext(PollDepsContext);
  const turns = useRunTurns(page.base, page.detail.run.id, true, page.live, deps);
  const drawn = new Set(page.activity.groups.flatMap((group) => (group.number == null ? [] : [`round-${group.number}`])));
  const covering = new Set(page.activity.groups.flatMap((group) => (group.kind === "covering_review" && group.number != null ? [group.number] : [])));
  const stages = stepperOf(page.detail, turns.data?.turns ?? [], page.phase, covering).map((stage) => ({
    ...stage,
    marks: stage.marks.map((mark) => (mark.anchor != null && drawn.has(mark.anchor) ? mark : { ...mark, anchor: null })),
  }));
  return <RunStepper stages={stages} />;
}

/** The header's sentence, a merged run's short sha in it linking to its
 *  commit. */
function Verdict({ sentence, run, merged }: { sentence: string; run: RunDetail["run"]; merged: boolean }) {
  const sha = merged ? shortSha(run.merge_sha) : "";
  const href = commitLink(run);
  const at = sha && href != null ? sentence.indexOf(sha) : -1;
  if (at < 0) return <>{sentence}</>;
  return (
    <>
      {sentence.slice(0, at)}
      <a href={href!} target="_blank" rel="noreferrer" className="font-mono text-[13px] text-inherit no-underline border-b border-rule-2 hover:border-ink-3">{sha}</a>
      {sentence.slice(at + sha.length)}
    </>
  );
}

/** A reverted merge's sentence: its short sha linking to its commit, the
 *  revert ticket linking to its page, and the revert's reason. */
function RevertedVerdict({ run, reverted, project }: { run: RunDetail["run"]; reverted: Reverted; project: string }) {
  const sha = shortSha(run.merge_sha);
  const href = commitLink(run);
  const shown = href == null ? <span className="font-mono text-[13px]">{sha}</span> : <a href={href} target="_blank" rel="noreferrer" className="font-mono text-[13px] text-inherit no-underline border-b border-rule-2 hover:border-ink-3">{sha}</a>;
  return (
    <>
      Merged into main{sha && <> as {shown}</>}, then reverted
      {reverted.by != null && (
        <>
          {" by "}
          <Link to="/tickets/$project/$key" params={{ project, key: reverted.by }} search={keepScope} className="text-act underline underline-offset-2 hover:text-ink">{reverted.by}</Link>
        </>
      )}
      {reverted.reason ? <>: “{reverted.reason}”</> : "."}
    </>
  );
}

const SUMMARY_FACT = "font-mono text-[12px] text-ink-3";
const MINUTE = 60_000;

const settledMinutes = (ms: number) => ms >= MINUTE && ms < 60 * MINUTE ? `${Math.floor(ms / MINUTE)} min` : formatSettled(ms);

/** A reverted merge's settled run: every stage finished as one unlabeled
 *  bar, then its pull request, review rounds, verify and duration. A merged
 *  run only merges past a passing verify, so a recorded verify passed. */
function RevertedSummary({ detail }: { detail: RunDetail }) {
  const { run, rounds } = detail;
  return (
    <div data-run-summary>
      <div aria-hidden="true" className="mt-5 flex gap-1">
        {STAGE_NAMES.map((name) => <span key={name} data-stage-done className="h-1 flex-1 rounded-[2px] bg-live/55" />)}
      </div>
      <p className="mt-4 flex flex-wrap gap-x-[18px] gap-y-1">
        {run.pr_url != null && <a href={run.pr_url} target="_blank" rel="noreferrer" className={`${SUMMARY_FACT} no-underline hover:text-ink`}>{prNumber(run.pr_url)}</a>}
        <span className={SUMMARY_FACT}>review <b className="font-semibold text-ink-2">{rounds.length} {rounds.length === 1 ? "round" : "rounds"}</b></span>
        {run.verify_ms != null && <span className={SUMMARY_FACT}>verify <b className="font-semibold text-ink-2">passed</b></span>}
        {run.ended_ms != null && <span className={SUMMARY_FACT}>{settledMinutes(run.ended_ms - run.started_ms)}</span>}
      </p>
    </div>
  );
}

/** A failed run's Scout target. */
const scoutOf = (page: Extract<RunPageFacts, { kind: "run" }>, project: string): ScoutTarget => {
  const { run } = page.detail;
  return { project, about: `run:${run.id}`, kind: "bug", ask: `Why did run ${run.id} of ${run.ticket} fail?`, off: page.actions ? null : ACTIONS_OFF_LINE };
};

/** The header's ⋯ menu: the run's `actions`, Scout for a failed run, Open
 *  ticket, Copy link, and Revert… for a merged run of a project that
 *  reports `reverts`. */
function RunMenu({ page, project, actions }: { page: Extract<RunPageFacts, { kind: "run" }>; project: string; actions: MenuItem[] }) {
  const { run } = page.detail;
  const scout = scoutOf(page, project);
  const openScout = useScoutThis(scout);
  const scoutItems: MenuItem[] = run.outcome === "failed" ? [{ label: "Scout", onSelect: openScout, disabled: scout.off != null, note: scout.off ?? undefined }] : [];
  const revertable = run.outcome === "merged" && page.host.status?.reverts === true;
  const revert = useRevertFlow(
    revertable ? { base: page.host.base, project, ticket: run.ticket, run: run.id, reverted: run.reverted, off: page.actions ? null : ACTIONS_OFF_LINE } : null,
    [...actions, ...scoutItems, ...usePlaceItems({ project, run: run.id }, { project, ticket: run.ticket })],
  );
  return (
    <div className="flex flex-col items-end gap-2">
      <MoreMenu label={`More for run ${run.id}`} items={revert.items} />
      {revert.layer}
    </div>
  );
}

export function RunPage() {
  const { project, run: param } = useParams({ from: "/runs/$project/$run" });
  const id = /^\d+$/.test(param) ? Number(param) : null;
  const page = useRunPage(project, id);
  const shown = id == null ? param : String(id);
  const label = `run ${shown}`;
  if (page.kind === "unknown_project") return <Notice label={label} text={`No project named ${project} answers this console.`} />;
  if (page.kind === "not_found") return <Notice label={label} text={`${project} has no run ${shown}.`} />;
  if (page.kind === "loading") return <Page label={label}><Loading /></Page>;
  const { run } = page.detail;
  const merged = page.state === "merged";
  const reverted = merged ? run.reverted : null;
  return (
    <Page label={label}>
      <header>
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
          <h1 className="m-0 font-sans text-[24px] font-semibold leading-[1.25] tracking-[-.015em] text-ink [text-wrap:balance]"><TitleText>{run.title}</TitleText></h1>
          <div className="flex flex-wrap items-start justify-end gap-2">
            <RunActions
              key={`${page.base}/${run.id}`}
              page={page}
              project={project}
              after={run.outcome === "failed" && <ScoutThis target={scoutOf(page, project)} />}
              menu={(actions) => <RunMenu page={page} project={project} actions={actions} />}
            />
          </div>
        </div>
        <div data-run-verdict className="mt-2.5 flex flex-wrap items-center gap-x-3.5 gap-y-2.5">
          <span className="flex">
            {reverted == null ? <StateDot state={page.state} hideWord /> : <RevertedGlyph />}
          </span>
          <p role="status" className="m-0 max-w-[64ch] font-sans text-[15px] leading-[1.45] text-ink">
            {reverted == null ? <Verdict sentence={page.sentence} run={run} merged={merged} /> : <RevertedVerdict run={run} reverted={reverted} project={project} />}
          </p>
          {reverted == null && page.agentMs != null && <TimeBox agentMs={page.agentMs} boxMs={page.boxMs} />}
        </div>
        {reverted == null ? (
          <>
            <Stepper page={page} />
            <RunFacts run={run} labels={page.labels} prFacts={page.prFacts} ready={page.state === "ready_to_merge"} merged={merged} />
          </>
        ) : (
          <RevertedSummary detail={page.detail} />
        )}
      </header>
      <div data-run-body className="@container mt-9 grid min-h-40 grid-cols-1 items-start gap-x-[52px] gap-y-8 min-[1180px]:grid-cols-[minmax(0,1fr)_340px]">
        <div className="flex min-w-0 flex-col gap-[30px]">
          <CriteriaList detail={page.detail} live={["running", "stale", "question"].includes(page.state)} />
          <section id="progress" aria-label="Progress" className="@container/progress scroll-mt-4">
            <h2 className="m-0 flex items-center gap-2.5 font-sans text-[13px] font-semibold leading-none text-ink">
              Progress
              {page.live && (
                <span data-live className="inline-flex items-center gap-1.5 font-mono text-[12px] font-normal text-live">
                  <span aria-hidden="true">
                    <StateDot state="running" hideWord tone="live" />
                  </span>
                  live
                </span>
              )}
            </h2>
            <RunActivity key={`${page.base}/${run.id}`} activity={page.activity} detail={page.detail} base={page.base} project={project} />
            <RawLog key={`raw ${page.base}/${run.id}`} run={run.id} lines={page.progress} />
          </section>
          <div>
            <RunTicket key={`ticket ${page.base}/${run.id}`} base={page.base} ticket={run.ticket} />
            <RunDetails key={`${page.base}/${run.id}`} project={project} base={page.base} detail={page.detail} live={page.live} />
          </div>
        </div>
        <AgentPanel label={page.labels?.implementer ?? "—"} state={page.agentState} live={page.live} notes={page.detail.steer_notes} asks={page.asks}>
          {!merged && <AgentCompose key={`${page.base}/${run.id}`} base={page.base} run={run.id} ticket={run.ticket} actions={page.actions} parked={page.phase === "awaiting_merge_approval"} live={page.stoppable} />}
        </AgentPanel>
      </div>
    </Page>
  );
}
