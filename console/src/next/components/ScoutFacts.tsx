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

/** "scout 31 · bug · demo · about run 812 · base a41c9e2 · started 11:46 PDT · 14m of a 30m budget · …", each fact the
 *  scout has, its helpers and tokens the first run's, then its newest follow-up pass. */
export function ScoutFacts({ detail, project, now, pass }: { detail: ScoutDetail; project: string; now: number; pass: { pass: number; ms: number | null } | null }) {
  const { scout } = detail;
  const tasks = detail.tasks.filter((task) => task.pass === 1);
  const head = (
    <>
      <span className={STRONG}>scout {scout.id}</span>
      <Dot />
      {scout.kind}
      <Dot />
      {project}
      <About subject={scout.subject} project={project} />
    </>
  );
  if (scout.phase === "queued" || scout.started_ms == null) {
    return <>{head}<Dot />queued {formatClock(scout.created_ms)} {zoneName(scout.created_ms)}</>;
  }
  const end = scout.ended_ms ?? now;
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
    pass == null ? null : <>pass {pass.pass}{pass.ms != null && <> <span className={STRONG}>{formatSettled(pass.ms)}</span></>}</>,
  ].filter((fact) => fact != null);
  return (
    <>
      {head}
      {facts.map((fact, index) => <span key={index}><Dot />{fact}</span>)}
    </>
  );
}
