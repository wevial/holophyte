import { Fragment, useId, useState } from "react";
import { useTick } from "../hooks/useTick";
import { describe, filterItems, orderByAge, splitPullRequests, type ProjectChoice } from "../lib/attention";
import { projectName } from "../lib/derive";
import { formatAge } from "../lib/format";
import { hostItems, sinceSeen, type HostRecord } from "../lib/hosts";
import { TICK_MS, type Fetch } from "../lib/poll";
import { prLabel } from "../lib/shipped";
import { groupByProject } from "../lib/runs";
import { PullRequestDetail } from "./PullRequestDetail";
import { RowActions } from "./RowActions";
import { PrFacts } from "./PrFacts";
import { TicketLink } from "./TicketLink";

/** Parked candidates, using the band's daemon clock and action wiring. */
export function PullRequestTable({ hosts, project, now, actionFetch, polls = 0, deps }: {
  hosts: HostRecord[];
  project: ProjectChoice;
  now: number;
  actionFetch?: Fetch;
  polls?: number;
  deps?: { fetch: Fetch };
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const id = useId();
  const localNow = useTick(TICK_MS);
  const { pullRequests } = splitPullRequests(filterItems(hosts.flatMap(host => hostItems(host, now)), "all", project));
  const rows = orderByAge(pullRequests.map(item => {
    const host = hosts.find(candidate => candidate.address === item.daemon);
    const status = host?.status;
    const description = describe(item, status?.thresholds ?? { heartbeat_stale_ms: 0, strikes: 0 }, {
      now: status ? status.now + sinceSeen(host?.seen_ms, localNow) : now,
    });
    return { item, host, description };
  }), row => row.description.ageMs);
  const paths = [...new Set([
    ...groupByProject(hosts.flatMap(host => host.status && !host.contract_error
      ? [{ base: host.base, status: host.status }] : [])).map(group => group.path),
    ...rows.map(row => String(row.item.project)),
  ])];
  const groups = paths.map(path => ({ path, rows: rows.filter(row => String(row.item.project) === path) }))
    .filter(group => group.rows.length > 0);
  const toggle = (key: string) => setExpanded(previous => {
    const next = new Set(previous);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  if (rows.length === 0) return null;
  return (
    <section id="pull-requests" aria-label="Pull requests" className="px-6 pb-6">
      <h2 className="text-[20px] font-semibold text-ink">Pull requests</h2>
      {groups.map(group => <div key={group.path} className="mt-4">
        <h3 className="text-[15px] font-semibold text-ink">{projectName(group.path)} · {group.rows.length}</h3>
        <div className="mt-3 overflow-x-auto rounded-[10px] border border-line bg-card shadow-card">
          <table aria-label={`${projectName(group.path)} pull requests`} className="w-full text-left text-[13px] text-body">
            <thead className="bg-card-header text-[12px] text-muted">
              <tr>{["Ticket", "Pull request", "Waiting for", "Waiting", "Actions"].map(label => (
                <th key={label} scope="col" className="px-4 py-[10px] font-semibold">{label}</th>
              ))}</tr>
            </thead>
            <tbody>{group.rows.map(({ item, host, description }, index) => {
              const key = `${String(item.daemon ?? "")}-pr_open-${String(item.run ?? item.ticket ?? index)}`;
              const detailId = `${id}-${encodeURIComponent(key)}`;
              const open = expanded.has(key);
              return <Fragment key={key}>
                <tr className="border-t border-line align-top">
                  <td className="px-4 py-3 font-mono">
                    {host && typeof item.run === "number" && <button type="button"
                      aria-label={`Details for ${description.ticket ?? `run ${item.run}`}`}
                      aria-expanded={open} aria-controls={detailId} onClick={() => toggle(key)}
                      className="mr-2 rounded-button px-1 text-muted focus-visible:outline-2 focus-visible:outline-link">
                      <span aria-hidden="true">{open ? "▾" : "▸"}</span>
                    </button>}
                    <TicketLink ticket={description.ticket ?? "—"} ticket_url={item.ticket_url} /></td>
                  <td className="px-4 py-3 font-mono text-link">{item.pr_url ? (
                    <a href={item.pr_url} target="_blank" rel="noopener noreferrer">{prLabel(item.pr_url).replace(/^PR /, "")}</a>
                  ) : "—"}</td>
                  <td className="px-4 py-3">
                    <p>{description.body}</p>
                    <PrFacts facts={description.facts} />
                    {description.meta && <p className="text-[12px] text-faint">{description.meta}</p>}
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 font-mono text-[12px] text-muted">{description.ageMs == null ? "" : formatAge(description.ageMs)}</td>
                  <td className="px-4 py-3"><RowActions kind="pr_open" actions={description.actions} ticket={description.ticket}
                    prUrl={item.pr_url} runId={typeof item.run === "number" ? item.run : undefined}
                    daemon={host ? { base: host.base, actions: host.status?.actions === true, fetch: actionFetch } : undefined} /></td>
                </tr>
                {open && host && typeof item.run === "number" && <tr id={detailId} className="border-t border-line">
                  <td colSpan={5} className="px-4 py-3">
                    <PullRequestDetail base={host.base} id={item.run} item={item}
                      now={host.status ? host.status.now + sinceSeen(host.seen_ms, localNow) : now}
                      polls={polls} deps={deps} />
                  </td>
                </tr>}
              </Fragment>;
            })}</tbody>
          </table>
        </div>
      </div>)}
    </section>
  );
}
