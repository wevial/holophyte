import { Link, useParams } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { TicketLink } from "../../components/TicketLink";
import type { RunTurn } from "../../lib/contract.gen";
import { formatClockSeconds, formatSpan } from "../../lib/format";
import { TurnArrow } from "../components/icons";
import { runPage } from "../run";
import { turnPage, turnTitles, useTurnPage } from "../turn";
import { CRUMB_LINK, Notice, Page, zoneName } from "./runShared";

function count(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

function duration(turn: RunTurn): string {
  return turn.seconds == null ? "—" : formatSpan(turn.seconds * 1000);
}

function Figure({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="min-w-0 border-rule-2 px-5 py-4 [&+div]:border-l max-[720px]:border-b max-[720px]:px-3">
      <dt className="font-mono text-[11px] uppercase tracking-[.12em] text-ink-3">{label}</dt>
      <dd className="m-0 mt-2 font-mono text-[15px] text-ink [overflow-wrap:anywhere]">{value}</dd>
      {sub && <dd className="m-0 mt-1.5 font-mono text-[12px] leading-[1.6] text-ink-3 [overflow-wrap:anywhere]">{sub}</dd>}
    </div>
  );
}

function TurnLink({ project, run, turn, next = false }: { project: string; run: number; turn?: RunTurn; next?: boolean }) {
  const label = next ? "Next turn" : "Previous turn";
  const icon: ReactNode = <TurnArrow next={next} />;
  if (!turn) return <a role="link" aria-label={label} aria-disabled="true" className="btn opacity-40" data-variant="quiet">{icon}</a>;
  return <Link {...turnPage(project, run, turn.id)} aria-label={label} className="btn" data-variant="quiet">{icon}</Link>;
}

export function TurnPage() {
  const { project, run: runParam, turn: turnParam } = useParams({ from: "/runs/$project/$run/turns/$turn" });
  const runId = /^\d+$/.test(runParam) && Number.isSafeInteger(Number(runParam)) ? Number(runParam) : null;
  const turnId = /^\d+$/.test(turnParam) && Number.isSafeInteger(Number(turnParam)) ? Number(turnParam) : null;
  const page = useTurnPage(project, runId, turnId);
  const shownRun = runId == null ? runParam : String(runId);
  const shownTurn = turnId == null ? turnParam : String(turnId);
  const label = `Turn transcript · run ${shownRun}`;
  if (page.kind === "unknown_project") return <Notice label={label} text={`No project named ${project} answers this console.`} />;
  if (page.kind === "not_found") return <Notice label={label} text={`${project} has no run ${shownRun}.`} />;
  if (page.kind === "turn_not_found") return <Notice label={label} text={`Run ${shownRun} has no turn ${shownTurn}.`} back={runId == null ? undefined : runPage(project, runId)} />;
  if (page.kind === "loading") return <Page label={label}><p role="status" className="m-0 font-mono text-[13px] text-ink-3">Asking {project} for run {shownRun}…</p></Page>;
  const { run } = page.detail;
  const { turn } = page;
  const titles = turnTitles(page.turns);
  const index = titles.findIndex((entry) => entry.turn.id === turn.id);
  const current = titles[index]!;
  const previous = titles[index - 1];
  const next = titles[index + 1];
  const title = current.title.charAt(0).toUpperCase() + current.title.slice(1);
  const cost = titles.slice(0, index + 1).reduce((sum, entry) => sum + (entry.turn.cost_usd ?? 0), 0);
  const clock = turn.seconds == null ? undefined : `${formatClockSeconds(turn.ended_ms - turn.seconds * 1000)} – ${formatClockSeconds(turn.ended_ms)} ${zoneName(turn.ended_ms)}`;
  const tokens = turn.input_tokens == null && turn.output_tokens == null ? "not recorded" : count((turn.input_tokens ?? 0) + (turn.output_tokens ?? 0));
  const usage = tokens === "not recorded" ? undefined : `${turn.input_tokens == null ? "not recorded" : count(turn.input_tokens)} in · ${turn.output_tokens == null ? "not recorded" : count(turn.output_tokens)} out`;
  return (
    <Page label={label}>
      <header>
        <nav aria-label="Breadcrumb" className="font-mono text-[12px] leading-[1.6] text-ink-3">
          <Link to="/runs" search={{}} className={CRUMB_LINK}>Runs</Link>{" / "}
          <span className="[&_a]:border-b [&_a]:border-rule [&_a]:no-underline"><TicketLink ticket={run.ticket} ticket_url={run.ticket_url} /></span>{" / "}
          <Link {...runPage(project, run.id)} className={CRUMB_LINK}>run {run.id}</Link>{" / "}
          <span>{current.ordinal || current.seat}</span>
        </nav>
        <div className="mt-3 flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <h1 className="m-0 font-sans text-[20px] font-semibold leading-[1.25] tracking-[-.005em] text-ink">{title}</h1>
            <p className="m-0 mt-2 font-read text-[15px] leading-[1.5] text-ink-2">{run.title}{previous?.turn.role === "review" && ` · back from review ${previous.ordinal}`}</p>
          </div>
          <div className="flex items-center gap-2">
            <TurnLink project={project} run={run.id} turn={previous?.turn} />
            <TurnLink project={project} run={run.id} turn={next?.turn} next />
            <Link {...runPage(project, run.id)} className="btn" data-variant="quiet">Back to the run</Link>
          </div>
        </div>
      </header>
      <dl className="m-0 mt-6 grid grid-cols-5 border border-rule bg-paper max-[1000px]:grid-cols-3 max-[720px]:grid-cols-2">
        <Figure label="Seat" value={current.seat} sub={run.host ?? project} />
        <Figure label="Model" value={turn.label ?? "not recorded"} sub={`${turn.route} route`} />
        <Figure label="Duration" value={duration(turn)} sub={clock} />
        <Figure label="Tokens" value={tokens} sub={usage} />
        <Figure label="Cost" value={turn.cost_usd == null ? "not recorded" : `$${turn.cost_usd.toFixed(2)}`} sub={`run so far $${cost.toFixed(2)}`} />
      </dl>
      <nav aria-label={`Turns of run ${run.id}`} className="mt-5 flex flex-wrap gap-2 border-b border-rule pb-4">
        {titles.map((entry) => (
          <Link key={entry.turn.id} {...turnPage(project, run.id, entry.turn.id)} aria-current={entry.turn.id === turn.id ? "page" : undefined}
            className={`flex flex-wrap items-center gap-x-3 gap-y-1 border px-3 py-2 font-mono text-[12px] no-underline ${entry.turn.id === turn.id ? "border-act text-act" : "border-rule text-ink-2 hover:text-ink"}`}>
            <span>{entry.title}</span><span className="text-ink-3">{duration(entry.turn)}</span>
          </Link>
        ))}
      </nav>
    </Page>
  );
}
