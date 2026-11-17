import { Link, useParams } from "@tanstack/react-router";
import { TicketLink } from "../components/TicketLink";
import type { RunTurn } from "../../lib/contract.gen";
import { formatClockSeconds, formatSpan } from "../../lib/format";
import { TitleText } from "../components/TitleText";
import { TurnEntries } from "../components/TurnEntries";
import { TurnArrow } from "../components/icons";
import { runPage } from "../run";
import { turnPage, turnTitles, useTurnPage } from "../turn";
import { CRUMB_LINK, Loading, Notice, Page, zoneName } from "./runShared";

function count(value: number): string {
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value);
}

function duration(turn: RunTurn): string {
  return turn.seconds == null ? "—" : formatSpan(turn.seconds * 1000);
}

function Figure({ label, value, sub, mono = false, subMono = mono }: { label: string; value: string; sub?: string; mono?: boolean; subMono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="font-sans text-[12px] leading-[1.4] text-ink-3">{label}</dt>
      <dd className={`m-0 mt-1 text-[14px] leading-[1.4] text-ink [overflow-wrap:anywhere] ${mono ? "font-mono" : "font-sans"}`}>{value}</dd>
      {sub && <dd className={`m-0 mt-0.5 text-[12px] leading-[1.5] text-ink-3 [overflow-wrap:anywhere] ${subMono ? "font-mono" : "font-sans"}`}>{sub}</dd>}
    </div>
  );
}

const ICON_BUTTON = "inline-flex size-7 items-center justify-center rounded-button text-ink-2";

function TurnLink({ project, run, turn, next = false }: { project: string; run: number; turn?: RunTurn; next?: boolean }) {
  const label = next ? "Next turn" : "Previous turn";
  const icon = <TurnArrow next={next} />;
  if (!turn) return <a role="link" aria-label={label} aria-disabled="true" className={`${ICON_BUTTON} cursor-not-allowed opacity-40`}>{icon}</a>;
  return <Link {...turnPage(project, run, turn.id)} aria-label={label} title={label}
    className={`${ICON_BUTTON} transition-colors duration-150 hover:bg-hover hover:text-ink motion-reduce:transition-none`}>{icon}</Link>;
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
  if (page.kind === "loading") return <Page label={label}><Loading /></Page>;
  const { run } = page.detail;
  const { turn } = page;
  const titles = turnTitles(page.turns);
  const index = titles.findIndex((entry) => entry.turn.id === turn.id);
  const current = titles[index]!;
  const previous = titles[index - 1];
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
            <p className="m-0 mt-1.5 font-sans text-[14px] leading-[1.5] text-ink-2"><TitleText>{run.title}</TitleText>{previous?.turn.role === "review" && ` · back from review ${previous.ordinal}`}</p>
          </div>
          <div className="flex items-center gap-1">
            <TurnLink project={project} run={run.id} turn={previous?.turn} />
            <TurnLink project={project} run={run.id} turn={titles[index + 1]?.turn} next />
            <Link {...runPage(project, run.id)} className="btn" data-variant="quiet">Back to the run</Link>
          </div>
        </div>
      </header>
      <dl className="m-0 mt-6 grid grid-cols-5 gap-x-6 gap-y-4 max-[1000px]:grid-cols-3 max-[720px]:grid-cols-2">
        <Figure label="Seat" value={current.seat} sub={run.host ?? project} />
        <Figure label="Model" value={turn.label ?? "not recorded"} sub={`${turn.route} route`} />
        <Figure label="Duration" value={duration(turn)} sub={clock} mono />
        <Figure label="Tokens" value={tokens} sub={usage} mono={turn.input_tokens != null || turn.output_tokens != null} />
        <Figure label="Cost" value={turn.cost_usd == null ? "not recorded" : `$${turn.cost_usd.toFixed(2)}`} sub={`run so far $${cost.toFixed(2)}`} mono={turn.cost_usd != null} subMono />
      </dl>
      <nav aria-label={`Turns of run ${run.id}`} className="mt-6 flex flex-wrap gap-0.5">
        {titles.map((entry) => (
          <Link key={entry.turn.id} {...turnPage(project, run.id, entry.turn.id)} aria-current={entry.turn.id === turn.id ? "page" : undefined}
            className={`inline-flex h-7 items-center gap-[7px] rounded-button px-2.5 font-sans text-[13px] font-medium leading-none no-underline ${entry.turn.id === turn.id ? "bg-selected text-ink" : "text-ink-2 hover:text-ink"}`}>
            <span>{entry.title}</span><span className="font-mono text-[12px] font-normal text-ink-3">{duration(entry.turn)}</span>
          </Link>
        ))}
      </nav>
      <TurnEntries key={turn.id} base={page.base} project={project} run={run.id} host={run.host} turn={turn} />
    </Page>
  );
}
