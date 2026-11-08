import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import type { ScoutDetail } from "../../lib/contract.gen";
import { formatClock, formatSettled } from "../../lib/format";
import { shortSha } from "../../lib/shipped";
import { zoneName } from "../pages/runShared";
import { backCount, stoppedCount } from "../scout";
import { aboutTarget } from "../scouts";

const ABOUT_LINK = "underline underline-offset-2 hover:text-ink";

/** The scout's subject, linked to its page or, for a pull request, its address; null without one. */
function aboutLink(subject: ScoutDetail["scout"]["subject"], project: string): ReactNode {
  const target = subject == null ? null : aboutTarget(subject);
  if (target == null) return null;
  return "url" in target ? <a href={target.url} target="_blank" rel="noreferrer" className={ABOUT_LINK}>{target.words}</a>
    : "ticket" in target ? <Link to="/tickets/$project/$key" params={{ project, key: target.ticket }} search={{}} className={ABOUT_LINK}>{target.words}</Link>
    : <Link to="/runs/$project/$run" params={{ project, run: target.run }} className={ABOUT_LINK}>{target.words}</Link>;
}

/** "Sat 02:23 PDT": `at` with its weekday, in the browser's zone. */
export function dayClock(at: number): string {
  return `${new Date(at).toLocaleDateString("en-US", { weekday: "short" })} ${formatClock(at)} ${zoneName(at)}`;
}

/** "research · 8m of 45m · Fri 17:50 PST · lead opus"; a queued scout's reads when it was queued instead. */
export function scoutMeta(detail: ScoutDetail, now: number): string {
  const { scout } = detail;
  const queued = scout.phase === "queued" || scout.started_ms == null;
  const when = queued
    ? [`queued ${dayClock(scout.created_ms)}`]
    : [`${formatSettled((scout.ended_ms ?? now) - scout.started_ms!)} of ${formatSettled(scout.budget.minutes * 60_000)}`, dayClock(scout.started_ms!)];
  return [scout.kind, ...when, ...(scout.lead == null ? [] : [`lead ${scout.lead}`])].join(" · ");
}

/** The run's base, helpers, tasks back and tokens, the first run's, then its subject and newest follow-up pass; null
 *  for a queued scout. */
export function ScoutRunFacts({ detail, project, pass }: {
  detail: ScoutDetail;
  project: string;
  pass: { pass: number; ms: number | null } | null;
}) {
  const { scout } = detail;
  if (scout.phase === "queued" || scout.started_ms == null) return null;
  const tasks = detail.tasks.filter((task) => task.pass === 1);
  const stopped = stoppedCount(tasks);
  const tokens = tasks.flatMap((task) => (task.tokens == null ? [] : [task.tokens]));
  const facts = ([
    ["base", scout.base_sha == null ? null : shortSha(scout.base_sha)],
    ["helpers", scout.helper == null ? null : `${scout.helper}${scout.swarm == null ? "" : ` ×${scout.swarm}`}`],
    ["back", tasks.length === 0 ? null : `${backCount(tasks)} of ${tasks.length}${stopped ? `, ${stopped} stopped` : ""}`],
    ["tokens", tokens.length === 0 ? null : `${Math.round(tokens.reduce((sum, each) => sum + each, 0) / 1000)}k`],
    ["about", aboutLink(scout.subject, project)],
    ["pass", pass == null ? null : pass.ms == null ? String(pass.pass) : `${pass.pass} · ${formatSettled(pass.ms)}`],
  ] satisfies [string, ReactNode][]).filter(([, value]) => value != null);
  return (
    <dl data-scout-facts className="m-0 mb-6 flex flex-wrap gap-x-5 gap-y-1.5 font-mono text-[12px] leading-[1.6] text-ink-2">
      {facts.map(([name, value]) => (
        <div key={name} className="flex gap-1.5">
          <dt className="text-ink-3">{name}</dt>
          <dd className="m-0 [overflow-wrap:anywhere]">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
