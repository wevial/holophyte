import { useTick } from "../hooks/useTick";
import { describe, filterItems, orderByAge, splitPullRequests, type ProjectChoice } from "../lib/attention";
import { projectName } from "../lib/derive";
import { formatAge } from "../lib/format";
import { hostItems, sinceSeen, type HostRecord } from "../lib/hosts";
import { TICK_MS, type Fetch } from "../lib/poll";
import { prLabel } from "../lib/shipped";
import { RowActions } from "./RowActions";
import { PrFacts } from "./PrFacts";
import { TicketLink } from "./TicketLink";

/** Parked candidates, using the band's daemon clock and action wiring. */
export function PullRequestTable({ hosts, project, now, actionFetch }: {
  hosts: HostRecord[];
  project: ProjectChoice;
  now: number;
  actionFetch?: Fetch;
}) {
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
  if (rows.length === 0) return null;
  return (
    <section id="pull-requests" aria-label="Pull requests" className="px-6 pb-6">
      <h2 className="text-[20px] font-semibold text-ink">Pull requests</h2>
      <div className="mt-3 overflow-x-auto rounded-[10px] border border-line bg-card shadow-card">
        <table className="w-full text-left text-[13px] text-body">
          <thead className="bg-card-header text-[12px] text-muted">
            <tr>{["Project", "Ticket", "Pull request", "Waiting for", "Waiting", "Actions"].map(label => (
              <th key={label} scope="col" className="px-4 py-[10px] font-semibold">{label}</th>
            ))}</tr>
          </thead>
          <tbody>{rows.map(({ item, host, description }, index) => (
            <tr key={`${String(item.daemon ?? "")}-pr_open-${String(item.run ?? item.ticket ?? index)}`} className="border-t border-line align-top">
              <td className="px-4 py-3 font-semibold text-ink">{projectName(String(item.project))}</td>
              <td className="px-4 py-3 font-mono"><TicketLink ticket={description.ticket ?? "—"} ticket_url={item.ticket_url} /></td>
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
          ))}</tbody>
        </table>
      </div>
    </section>
  );
}
