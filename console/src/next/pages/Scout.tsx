import { Link, useParams } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { formatClock, formatSettled } from "../../lib/format";
import { shortSha } from "../../lib/shipped";
import { SECTION_LABEL } from "../components/Criteria";
import { ScoutActivity } from "../components/ScoutActivity";
import { ScoutActions } from "../components/ScoutActions";
import { ScoutReport } from "../components/ScoutReport";
import { ScoutStop } from "../components/ScoutStop";
import { ScoutThread } from "../components/ScoutThread";
import { StageChip } from "../components/StageChip";
import { StateDot } from "../components/StateDot";
import { backCount, SCOUT_DOTS, stoppedCount, tokenWords, useScoutPage } from "../scout";
import { hasThread } from "../scoutThread";
import { CRUMB_LINK, Notice, Page, zoneName } from "./runShared";

const STRONG = "font-medium text-ink-2";

function Dot() {
  return <>{" · "}</>;
}

/** "scout 31 · bug · demo · base a41c9e2 · started 11:46 PDT · 14m of a 30m budget · …", each fact the scout has. */
function Facts({ detail, project, now, live }: { detail: ScoutDetail; project: string; now: number; live: boolean }) {
  const { scout, tasks } = detail;
  const head = (
    <>
      <span className={STRONG}>scout {scout.id}</span>
      <Dot />
      {scout.kind}
      <Dot />
      {project}
    </>
  );
  if (scout.phase === "queued" || scout.started_ms == null) {
    return <>{head}<Dot />queued {formatClock(scout.created_ms)} {zoneName(scout.created_ms)}</>;
  }
  const end = live || scout.ended_ms == null ? now : scout.ended_ms;
  const stopped = stoppedCount(tasks);
  const tokens = tasks.flatMap((task) => (task.tokens == null ? [] : [task.tokens]));
  const facts = [
    scout.base_sha == null ? null : `base ${shortSha(scout.base_sha)}`,
    `started ${formatClock(scout.started_ms)} ${zoneName(scout.started_ms)}`,
    <><span className={STRONG}>{formatSettled(end - scout.started_ms)}</span> of a {formatSettled(scout.budget.minutes * 60_000)} budget</>,
    scout.lead == null ? null : `lead ${scout.lead}`,
    scout.helper == null ? null : `helpers ${scout.helper}${scout.swarm == null ? "" : ` ×${scout.swarm}`}`,
    tasks.length === 0 ? null : `${backCount(tasks)} of ${tasks.length} back${stopped ? `, ${stopped} stopped` : ""}`,
    tokens.length === 0 ? null : tokenWords(tokens.reduce((sum, each) => sum + each, 0)),
  ].filter((fact) => fact != null);
  return (
    <>
      {head}
      {facts.map((fact, index) => <span key={index}><Dot />{fact}</span>)}
    </>
  );
}

export function ScoutPage() {
  const { project, scout: param } = useParams({ from: "/scouts/$project/$scout" });
  const id = /^\d+$/.test(param) ? Number(param) : null;
  const page = useScoutPage(project, id);
  const heading = useRef<HTMLHeadingElement>(null);
  const stopSent = useRef(false);
  const composeRef = useRef<HTMLTextAreaElement>(null);
  const stoppable = page.kind === "scout" && page.stoppable;
  useEffect(() => {
    if (stoppable || !stopSent.current) return;
    stopSent.current = false;
    if (document.activeElement === document.body) heading.current?.focus();
  }, [stoppable]);
  const shown = id == null ? param : String(id);
  const label = `scout ${shown}`;
  const notice = (text: string) => <Notice label={label} text={text} back={{ to: "/scouts", search: { project } }} backLabel="Back to Scouts" />;
  if (page.kind === "unknown_project") return notice(`No project named ${project} answers this console.`);
  if (page.kind === "not_found") return notice(`${project} has no scout ${shown}.`);
  if (page.kind === "loading") {
    return (
      <Page label={label}>
        <p role="status" className="m-0 font-mono text-[13px] text-ink-3">
          Asking {project} for scout {shown}…
        </p>
      </Page>
    );
  }
  const { scout } = page.detail;
  const dot = SCOUT_DOTS[page.state];
  const thread = hasThread(page.detail);
  return (
    <Page label={label}>
      <header className="border-b border-rule pb-3">
        <nav aria-label="Breadcrumb" className="font-mono text-[12px] leading-none text-ink-3">
          <Link to="/scouts" search={{}} className={CRUMB_LINK}>Scouts</Link>
          {" / "}
          <Link to="/scouts" search={{ project }} className={CRUMB_LINK}>{project}</Link>
          {" / "}
          <span>scout {scout.id}</span>
        </nav>
        <div className="mt-2 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
          <h1 ref={heading} tabIndex={-1} className="m-0 max-w-[64ch] font-sans text-[20px] font-semibold leading-[1.25] tracking-[-.005em] text-ink [overflow-wrap:anywhere]">{scout.ask}</h1>
          {page.stoppable && <ScoutStop key={`${page.base}/${scout.id}`} base={page.base} project={project} scout={scout.id} actions={page.actions} onSent={() => (stopSent.current = true)} />}
        </div>
        <p data-scout-facts className="m-0 mt-3 font-mono text-[12px] leading-[1.7] text-ink-3 [overflow-wrap:anywhere]">
          <Facts detail={page.detail} project={project} now={page.now} live={page.live} />
        </p>
        <div className="mt-[18px] flex items-start gap-3.5">
          <span className="mt-1">
            <StateDot state={dot} label={page.state} hideWord />
          </span>
          <p role="status" className="m-0 max-w-[64ch] font-sans text-[17px] leading-[1.45] text-ink">
            {page.sentence}
          </p>
        </div>
        <div className="mt-[18px] flex flex-wrap items-center gap-x-3 gap-y-2.5">
          {thread && scout.phase === "done" && <a href="#thread" className="btn" data-variant="quiet" onClick={(event) => {
            event.preventDefault();
            composeRef.current?.focus();
            composeRef.current?.scrollIntoView({ block: "nearest" });
          }}>Ask or steer</a>}
          <span className="font-mono text-[11px] font-medium uppercase leading-none tracking-[.12em] text-ink-3">Stage</span>
          <span role="group" aria-label="Stage">
            <StageChip label={page.stage.label} state={dot} sub={page.stage.sub} />
          </span>
        </div>
      </header>
      <div data-scout-body className="@container mt-3.5 flex flex-wrap border border-rule bg-paper">
        <div className={`min-w-0 flex-[999_1_560px] px-8 pb-8 pt-[22px] max-[720px]:px-4 ${thread ? "@[1000px]:border-r @[1000px]:border-rule" : ""}`}>
          <section aria-label="Progress">
            <h2 className={`m-0 ${SECTION_LABEL}`}>
              Progress
              {page.live && (
                <span data-live className="inline-flex items-center gap-1.5 tracking-[.1em] text-live">
                  <span aria-hidden="true">
                    <StateDot state="running" hideWord tone="live" />
                  </span>
                  live
                </span>
              )}
            </h2>
            <ScoutActivity key={`${page.base}/${scout.id}`} groups={page.activity} now={page.live ? page.now : (scout.ended_ms ?? page.now)} />
          </section>
          <ScoutReport key={`${page.base}/${scout.id}`} reports={page.detail.reports} baseSha={scout.base_sha} />
          <ScoutActions key={`${page.base}/${scout.id}/actions`} items={page.detail.actions} base={page.base} project={project} scout={scout.id} actions={page.actions} />
        </div>
        {thread && <ScoutThread key={`${page.base}/${scout.id}/thread`} base={page.base} detail={page.detail} actions={page.actions} ref={composeRef} />}
      </div>
    </Page>
  );
}
