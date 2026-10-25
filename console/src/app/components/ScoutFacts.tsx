import { Link } from "@tanstack/react-router";
import type { ScoutDetail } from "../../lib/contract.gen";
import { formatClock, formatSettled } from "../../lib/format";
import { shortSha } from "../../lib/shipped";
import { zoneName } from "../pages/runShared";
import { backCount, stoppedCount, tokenWords } from "../scout";
import { aboutTarget } from "../scouts";

const STRONG = "font-medium text-ink-2";

function Dot() {
  return <>{" · "}</>;
}

const ABOUT_LINK = "underline underline-offset-2 hover:text-ink";

/** "about run 812", linked to its subject's page or, for a pull request, its address. */
function About({ subject, project }: { subject: ScoutDetail["scout"]["subject"]; project: string }) {
  const target = subject == null ? null : aboutTarget(subject);
  if (target == null) return null;
  const link = "url" in target ? <a href={target.url} target="_blank" rel="noreferrer" className={ABOUT_LINK}>about {target.words}</a>
    : "ticket" in target ? <Link to="/tickets/$project/$key" params={{ project, key: target.ticket }} search={{}} className={ABOUT_LINK}>about {target.words}</Link>
    : <Link to="/runs/$project/$run" params={{ project, run: target.run }} className={ABOUT_LINK}>about {target.words}</Link>;
  return <><Dot />{link}</>;
}

/** "Sat 02:23 PDT": `at` with its weekday, in the browser's zone. */
export function dayClock(at: number): string {
  return `${new Date(at).toLocaleDateString("en-US", { weekday: "short" })} ${formatClock(at)} ${zoneName(at)}`;
}

/** "scout 31 · bug · demo · about run 812 · base d149ad5 · started Sat 02:23 PDT", each fact the scout has; a queued
 *  scout's ends on when it was queued. */
export function ScoutWhat({ detail, project }: { detail: ScoutDetail; project: string }) {
  const { scout } = detail;
  const queued = scout.phase === "queued" || scout.started_ms == null;
  return (
    <>
      <span className={STRONG}>scout {scout.id}</span>
      <Dot />
      {scout.kind}
      <Dot />
      {project}
      <About subject={scout.subject} project={project} />
      {!queued && scout.base_sha != null && <><Dot />base {shortSha(scout.base_sha)}</>}
      <Dot />
      {queued ? `queued ${dayClock(scout.created_ms)}` : `started ${dayClock(scout.started_ms!)}`}
    </>
  );
}

/** "3m of 20m · lead opus · helpers luna ×3 · 12 of 12 back · 280k tok · pass 2 4m", its helpers and tokens the first
 *  run's, then its newest follow-up pass; null for a queued scout. */
export function ScoutCost({ detail, now, pass }: { detail: ScoutDetail; now: number; pass: { pass: number; ms: number | null } | null }) {
  const { scout } = detail;
  if (scout.phase === "queued" || scout.started_ms == null) return null;
  const tasks = detail.tasks.filter((task) => task.pass === 1);
  const end = scout.ended_ms ?? now;
  const stopped = stoppedCount(tasks);
  const tokens = tasks.flatMap((task) => (task.tokens == null ? [] : [task.tokens]));
  const facts = [
    <><span className={STRONG}>{formatSettled(end - scout.started_ms)}</span> of {formatSettled(scout.budget.minutes * 60_000)}</>,
    scout.lead == null ? null : `lead ${scout.lead}`,
    scout.helper == null ? null : `helpers ${scout.helper}${scout.swarm == null ? "" : ` ×${scout.swarm}`}`,
    tasks.length === 0 ? null : `${backCount(tasks)} of ${tasks.length} back${stopped ? `, ${stopped} stopped` : ""}`,
    tokens.length === 0 ? null : tokenWords(tokens.reduce((sum, each) => sum + each, 0)),
    pass == null ? null : <>pass {pass.pass}{pass.ms != null && <> <span className={STRONG}>{formatSettled(pass.ms)}</span></>}</>,
  ].filter((fact) => fact != null);
  return facts.map((fact, index) => <span key={index}>{index > 0 && <Dot />}{fact}</span>);
}
