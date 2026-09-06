import { useState } from "react";
import {
  CHIP_LABELS,
  KINDS,
  countsByKind,
  describe,
  filterItems,
  oldest,
  type KindFilter,
  type ProjectChoice,
} from "../lib/attention";
import { projectName } from "../lib/derive";
import { formatAge } from "../lib/format";
import { UNREACHABLE, hostItems, type HostRecord } from "../lib/hosts";
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

/** The band that opens the Now view: what needs a human across every host
 *  in view, each item stamped with its daemon's project, plus one critical
 *  row per daemon that stopped answering. `now` is the console's clock. */
export function NeedsYou({ hosts, project, now }: { hosts: HostRecord[]; project: ProjectChoice; now: number }) {
  const [kind, setKind] = useState<KindFilter>("all");
  const [expanded, setExpanded] = useState(false);

  const stamped: AttentionItem[] = hosts.flatMap((host) => hostItems(host, now));
  const mine = filterItems(stamped, "all", project);
  const counts = countsByKind(mine);
  const shown = filterItems(mine, kind, project);
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
            {rows.map((item, index) => (
              <AttentionRow
                key={`${String(item.daemon ?? "")}-${item.kind}-${String(item.run ?? item.ticket ?? index)}`}
                kind={item.kind}
                project={projectName(String(item.project))}
                description={describeRow(item)}
              />
            ))}
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
