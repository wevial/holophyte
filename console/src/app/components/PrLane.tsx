import { useId, useState } from "react";
import { ageOf, orderByAge, prTitle, type NeedsYouEntry, type ReadinessMap } from "../../lib/attention";
import { formatAge } from "../../lib/format";
import type { HostRecord } from "../../lib/hosts";
import { runKey } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { prWait } from "../prRow";
import { stateOf } from "../state";
import { PrRowDetail } from "./PrRowDetail";
import { StateDot } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";

type Host = HostRecord<ContractBodies>;

export interface WaitingRow {
  item: AttentionItem;
  host: Host;
  ageMs: number | null;
}

/** The branching pull request mark, drawn on a 16 px grid. */
export function PullRequestGlyph() {
  return (
    <>
      <circle cx="4.5" cy="3.5" r="1.6" />
      <circle cx="4.5" cy="12.5" r="1.6" />
      <circle cx="11.5" cy="12.5" r="1.6" />
      <path d="M4.5 5.1v5.8M11.5 10.9V6.5a2 2 0 0 0-2-2H7.4M8.8 3 7.3 4.5 8.8 6" />
    </>
  );
}

/** The runs parked on a pull request that are not Needs you items, oldest first. */
export function waitingRows(hosts: Host[], items: AttentionItem[], needs: NeedsYouEntry[], now: number): WaitingRow[] {
  const needed = new Set(needs.map((entry) => entry.item));
  const rows = items.flatMap((item) => {
    const host = hosts.find((candidate) => candidate.key === item.daemon);
    return needed.has(item) || host == null ? [] : [{ item, host, ageMs: ageOf(item, now) }];
  });
  return orderByAge(rows, (row) => row.ageMs);
}

const runOf = (item: AttentionItem) => (typeof item.run === "number" ? item.run : null);

function WaitingLine({ row, readiness }: { row: WaitingRow; readiness: ReadinessMap }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const { item, host } = row;
  const run = runOf(item);
  const ready = run == null ? undefined : readiness.get(runKey(host.key, run));
  const state = stateOf({ attention: item, readiness: ready });
  const wait = prWait(state, item, row.ageMs, ready?.reason);
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const title = prTitle(item) ?? "";
  const label = typeof item.pr_url === "string" ? prLabel(item.pr_url) : null;
  const shown = open && run != null;
  return (
    <li data-pr-row data-open={shown || undefined} className="border-b border-rule-2">
      <div data-row className="grid min-h-10 grid-cols-[16px_88px_minmax(0,1fr)_auto] items-center gap-x-3 px-2.5 font-mono text-[12px] text-ink-3 hover:bg-paper-2 max-[720px]:grid-cols-[16px_minmax(0,1fr)_auto]">
        <StateDot state={state} hideWord label={wait.status} />
        <span className="truncate text-ink-2 max-[720px]:hidden">{ticket}</span>
        <button
          type="button"
          aria-expanded={run == null ? undefined : open}
          aria-controls={run == null ? undefined : detailId}
          disabled={run == null}
          aria-label={`${ticket} ${plainTitle(title)}, ${wait.line === "" ? wait.status : `${wait.status}, ${wait.line}`}`}
          onClick={() => setOpen(!open)}
          className="min-h-10 min-w-0 truncate text-left font-read text-[14.5px] font-medium text-ink"
        >
          <TitleText>{title}</TitleText>
        </button>
        <span className="flex items-center gap-3 whitespace-nowrap">
          {label != null && (
            <a href={item.pr_url!} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-ink-3 hover:text-ink">
              <svg viewBox="0 0 16 16" width={12} height={12} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="size-3">
                <PullRequestGlyph />
              </svg>
              {label.replace(/^PR (?=#)/, "")}
            </a>
          )}
          {row.ageMs != null && <span data-age>{formatAge(row.ageMs)}</span>}
        </span>
      </div>
      {shown && (
        <div id={detailId} className="border-t border-dashed border-rule-2 py-4 pl-[129px] pr-2.5 max-[720px]:px-2.5">
          <PrRowDetail host={host} run={run} item={item} ready={state === "ready_to_merge"} bypass={ready?.reason === "review_bypassable"} />
        </div>
      )}
    </li>
  );
}

/** A hairline row per run parked on its pull request; a row with a run opens onto its detail. */
export function WaitingRows({ rows, readiness }: { rows: WaitingRow[]; readiness: ReadinessMap }) {
  return (
    <ul aria-label="Waiting runs">
      {rows.map((row, index) => (
        <WaitingLine key={`${row.host.key} ${row.item.pr_url ?? index}`} row={row} readiness={readiness} />
      ))}
    </ul>
  );
}
