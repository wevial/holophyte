import { useState } from "react";
import {
  CHIP_LABELS,
  KINDS,
  collapseFailed,
  countsByKind,
  describe,
  filterItems,
  oldest,
  type BandEntry,
  type KindFilter,
  type ProjectChoice,
} from "../lib/attention";
import { projectName } from "../lib/derive";
import { formatAge } from "../lib/format";
import { UNREACHABLE, hostItems, type HostRecord } from "../lib/hosts";
import type { Fetch } from "../lib/poll";
import type { Ledgers } from "../hooks/useLedger";
import { threadFor } from "../lib/threads";
import type { AttentionItem } from "../lib/types";
import { AttentionRow } from "./AttentionRow";
import { Chip } from "./Chip";

/** Rows shown before "Show all N". */
export const ROW_CAP = 4;

/** The level the band names when nothing needs a human: the worst any
 *  host reports, "none" with no answer yet. */
function bandLevel(hosts: HostRecord[]): string {
  const order = ["none", "working", "attention", "critical"];
  return hosts.reduce((worst, host) => {
    const level = host.attention?.level ?? "none";
    return order.indexOf(level) > order.indexOf(worst) ? level : worst;
  }, "none");
}

/** The key one row toggles its card by: its run for a question, its
 *  ticket for a failed ticket's attempts. */
function rowKey({ item, attempts }: BandEntry, index: number): string {
  const tail = attempts ? item.ticket : (item.run ?? item.ticket);
  return `${String(item.daemon ?? "")}-${item.kind}-${String(tail ?? index)}`;
}

/** The band that opens the Now view: what needs a human across every host
 *  in view, each item stamped with its daemon's project, plus one critical
 *  row per daemon that stopped answering. `now` is the console's clock.
 *  `ledgers` is each daemon's `/ledger` answer by address, its threads
 *  keyed by ticket; a question row whose daemon has one opens its thread,
 *  one thread at a time, and a band without any renders as before. The
 *  `failed` items of one ticket are one row wearing an attempts badge; it
 *  opens the attempts card the way a question opens its thread, and the
 *  band counts the ticket once. */
export function NeedsYou({
  hosts,
  project,
  now,
  ledgers = {},
  actionFetch,
}: {
  hosts: HostRecord[];
  project: ProjectChoice;
  now: number;
  ledgers?: Ledgers;
  /** The `fetch` action buttons post with; the page's own by default. */
  actionFetch?: Fetch;
}) {
  const [kind, setKind] = useState<KindFilter>("all");
  const [expanded, setExpanded] = useState(false);
  const [openRow, setOpenRow] = useState<string | null>(null);

  const stamped: AttentionItem[] = hosts.flatMap((host) => hostItems(host, now));
  const entries = collapseFailed(filterItems(stamped, "all", project));
  const mine = entries.map((entry) => entry.item);
  const counts = countsByKind(mine);
  const shown = entries.filter((entry) => filterItems([entry.item], kind, project).length > 0);
  const rows = expanded ? shown : shown.slice(0, ROW_CAP);
  const chooseKind = (next: KindFilter) => {
    setKind(next);
    setExpanded(false);
  };

  /** An item's row text against its own daemon's thresholds and clock; the
   *  console's clock for the unreachable row, whose "last seen" is local. */
  const describeRow = (item: AttentionItem) => {
    const status = hosts.find((candidate) => candidate.address === item.daemon)?.status;
    if (item.kind === UNREACHABLE || !status) {
      return describe(item, { heartbeat_stale_ms: 0, strikes: 0 }, { now });
    }
    return describe(item, status.thresholds, { now: status.now, runs: status.runs });
  };
  const eldest = oldest(mine, describeRow);

  /** The daemon a row's buttons post to: the host that served the item,
   *  with whether its `/status` advertised `actions`; undefined for the
   *  unreachable row, whose daemon is not answering. */
  const daemonOf = (item: AttentionItem) => {
    const host = hosts.find((candidate) => candidate.address === item.daemon);
    if (!host || item.kind === UNREACHABLE) return undefined;
    return { base: host.base, actions: host.status?.actions === true, fetch: actionFetch };
  };

  /** A question row's thread from its daemon's ledger; undefined for any
   *  other kind or a daemon without `/ledger`. */
  const threadOf = (item: AttentionItem, key: string) => {
    if (item.kind !== "blocked") return undefined;
    const ledger = ledgers[String(item.daemon ?? "")];
    if (!ledger || ledger.absent) return undefined;
    return {
      rows: threadFor(ledger.threads[String(item.ticket ?? "")] ?? [], item),
      open: openRow === key,
      onToggle: () => setOpenRow((previous) => (previous === key ? null : key)),
    };
  };

  /** A failed ticket's attempts card, when it failed more than once;
   *  it shares the one open slot with the question threads. */
  const attemptsOf = (entry: BandEntry, key: string) => {
    if (!entry.attempts) return undefined;
    return {
      runs: entry.attempts,
      open: openRow === key,
      onToggle: () => setOpenRow((previous) => (previous === key ? null : key)),
    };
  };

  return (
    <section
      aria-label="Needs you"
      className="border-b border-needs-you-border bg-needs-you-bg px-6 pt-[18px]"
    >
      {mine.length === 0 ? (
        <div className="pb-[18px]">
          <p className="text-[20px] font-semibold text-ink">Nothing needs you</p>
          <p className="font-mono text-[13px] text-muted">{bandLevel(hosts)}</p>
        </div>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span data-count className="font-mono text-[34px] font-bold leading-none text-ink">
              {mine.length}
            </span>
            <span className="text-[20px] font-semibold text-ink">
              {mine.length === 1 ? "thing needs you" : "things need you"}
            </span>
            {eldest && (
              <span className="text-[13px] text-muted">
                oldest {formatAge(eldest.ageMs)}
                {eldest.ticket ? ` · ${eldest.ticket}` : ""}
              </span>
            )}
          </div>
          <div role="group" aria-label="Kinds" className="mt-3 flex flex-wrap gap-2">
            {(["all", ...KINDS] as KindFilter[])
              .filter((candidate) => candidate === "all" || counts[candidate] > 0)
              .map((candidate) => (
                <Chip
                  key={candidate}
                  label={CHIP_LABELS[candidate]}
                  count={counts[candidate]}
                  selected={kind === candidate}
                  onClick={() => chooseKind(candidate)}
                />
              ))}
          </div>
          <ul className="mt-3">
            {rows.map((entry, index) => {
              const { item } = entry;
              const key = rowKey(entry, index);
              return (
                <AttentionRow
                  key={key}
                  kind={item.kind}
                  project={projectName(String(item.project))}
                  description={describeRow(item)}
                  thread={threadOf(item, key)}
                  attempts={attemptsOf(entry, key)}
                  prUrl={item.pr_url}
                  daemon={daemonOf(item)}
                />
              );
            })}
          </ul>
          {shown.length > ROW_CAP && (
            <button
              type="button"
              onClick={() => setExpanded((previous) => !previous)}
              className="mb-[18px] mt-1 text-[13px] font-semibold text-needs-you-link"
            >
              {expanded ? "Show fewer" : `Show all ${shown.length}`}
            </button>
          )}
          {shown.length <= ROW_CAP && <div className="pb-[18px]" />}
        </>
      )}
    </section>
  );
}
