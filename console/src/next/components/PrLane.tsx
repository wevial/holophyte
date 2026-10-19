import { useId, useState } from "react";
import { PrFacts } from "../../components/PrFacts";
import { describe, entryKey, orderByAge, prTitle, type Fact, type NeedsYouEntry, type ReadinessMap } from "../../lib/attention";
import { projectName } from "../../lib/derive";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { runKey } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { PullRequestFacts } from "../../lib/contract.gen";
import type { AttentionItem } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { prWait, rowFacts } from "../prRow";
import { stateOf } from "../state";
import { LaneHead } from "./LaneHead";
import { PrRowDetail } from "./PrRowDetail";
import { TONE_TEXT } from "./StateDot";

type Host = HostRecord<ContractBodies>;

const ROW_GRID =
  "grid min-h-[66px] grid-cols-[70px_84px_minmax(0,1fr)_150px] items-center gap-x-5 gap-y-1.5 px-5 py-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 transition-colors duration-(--motion-colour) hover:bg-paper-2 max-[820px]:grid-cols-[minmax(0,1fr)] @max-[720px]/ov-col:grid-cols-[minmax(0,1fr)]";
const ROW_BUTTON =
  "min-h-11 col-span-2 grid grid-cols-subgrid items-center gap-x-5 gap-y-1.5 text-left max-[820px]:col-span-1 @max-[720px]/ov-col:col-span-1 max-[820px]:grid-cols-[auto_minmax(0,1fr)] @max-[720px]/ov-col:grid-cols-[auto_minmax(0,1fr)]";
const DETAIL =
  "border-t border-dashed border-rule-2 py-4 pl-[calc(20px+70px+20px+84px+20px)] pr-5 max-[820px]:px-5 @max-[720px]/ov-col:px-5";

interface PrRow {
  item: AttentionItem;
  ageMs: number | null;
  facts: Fact[];
}

interface PrLaneGroup {
  host: Host;
  name: string;
  rows: PrRow[];
}

export function prLaneGroups(hosts: Host[], items: AttentionItem[], now: number): PrLaneGroup[] {
  return hosts.flatMap((host) => {
    const status = host.status;
    if (!status) return [];
    const clock = { now: status.now + sinceSeen(host.seen_ms, now) };
    const rows = items
      .filter((item) => item.daemon === host.key)
      .map((item) => {
        const pr = item.pr != null && typeof item.pr === "object" ? (item.pr as PullRequestFacts) : null;
        return { item, ageMs: describe(item, status.thresholds, clock).ageMs, facts: rowFacts(pr) };
      });
    if (rows.length === 0) return [];
    return [{ host, name: projectName(status.project), rows: orderByAge(rows, (row) => row.ageMs) }];
  });
}

function PrLaneRow({
  row,
  host,
  readiness,
  needIndex,
  onShow,
}: {
  row: PrRow;
  host: Host;
  readiness: ReadinessMap;
  needIndex: number;
  onShow: (index: number) => void;
}) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const { item, facts } = row;
  const run = typeof item.run === "number" ? item.run : null;
  const state = stateOf({ attention: item, readiness: run == null ? undefined : readiness.get(runKey(host.key, run)) });
  const wait = prWait(state, item, row.ageMs);
  const word = wait.line === "" ? wait.status : `${wait.status}, ${wait.line}`;
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const label = typeof item.pr_url === "string" ? prLabel(item.pr_url) : "PR";
  const title = prTitle(item) ?? "";
  return (
    <li data-pr-row className="border-rule-2 [&+&]:border-t">
      <div data-row className={ROW_GRID}>
        <div className="col-span-3 grid grid-cols-subgrid items-center gap-x-5 gap-y-1.5 max-[820px]:col-span-1 @max-[720px]/ov-col:col-span-1 max-[820px]:grid-cols-[auto_minmax(0,1fr)] @max-[720px]/ov-col:grid-cols-[auto_minmax(0,1fr)]">
          <a
            href={item.pr_url ?? undefined}
            target="_blank"
            rel="noreferrer"
            onClick={(event) => event.stopPropagation()}
            className="justify-self-start whitespace-nowrap border-b border-rule text-ink hover:border-ink hover:text-ink max-[820px]:self-start @max-[720px]/ov-col:self-start"
          >
            {label}
          </a>
          <button
            type="button"
            aria-expanded={run == null ? undefined : open}
            aria-controls={run == null ? undefined : detailId}
            disabled={run == null}
            aria-label={`${label}, ${ticket} ${title}, ${facts.map((fact) => fact.label).join(" · ")}, ${word}`}
            onClick={() => setOpen(!open)}
            className={ROW_BUTTON}
          >
            <span className="whitespace-nowrap text-ink-2 max-[820px]:self-start max-[820px]:pt-[3px] @max-[720px]/ov-col:self-start @max-[720px]/ov-col:pt-[3px]">{ticket}</span>
            <span className="flex min-w-0 flex-col gap-1">
              <span className="font-read text-[15px] font-medium leading-[1.35] text-ink">{title}</span>
              <PrFacts facts={facts} form="line" />
            </span>
          </button>
        </div>
        <span className="flex flex-col items-end gap-1 text-right max-[820px]:flex-row @max-[720px]/ov-col:flex-row max-[820px]:flex-wrap @max-[720px]/ov-col:flex-wrap max-[820px]:items-baseline @max-[720px]/ov-col:items-baseline max-[820px]:gap-x-3 @max-[720px]/ov-col:gap-x-3 max-[820px]:text-left @max-[720px]/ov-col:text-left">
          <span data-pr-state className="flex flex-col items-end gap-0.5 max-[820px]:items-start @max-[720px]/ov-col:items-start">
            <span data-pr-wait className={`font-sans text-[14px] font-semibold leading-[1.3] ${TONE_TEXT[wait.tone]}`}>{wait.status}</span>
            {wait.line !== "" && <span data-pr-since className="whitespace-nowrap font-mono text-[12px] text-ink-3">{wait.line}</span>}
          </span>
          {needIndex >= 0 && (
            <button
              type="button"
              aria-label={`Show ${ticket} in Needs you`}
              onClick={() => onShow(needIndex)}
              className="min-h-11 font-semibold text-need hover:underline"
            >
              needs you
            </button>
          )}
        </span>
      </div>
      {open && run != null && (
        <div id={detailId} className={DETAIL}>
          <PrRowDetail base={host.base} project={host.status?.project ?? ""} run={run} item={item} actions={host.status?.actions === true} />
        </div>
      )}
    </li>
  );
}

/** One project's parked pull requests, a row each that opens onto its
 *  detail; a row also in Needs you points back to it. */
function PrLane({ group, readiness, needs, onShow }: { group: PrLaneGroup; readiness: ReadinessMap; needs: NeedsYouEntry[]; onShow: (index: number) => void }) {
  const status = group.host.status!;
  return (
    <div role="group" aria-label={group.name}>
      <LaneHead name={group.name} held={status.admission === "held"} holdNote={status.hold_note} meta={`${group.rows.length} open`} />
      <ul className="border border-rule bg-paper">
        {group.rows.map((row, index) => (
          <PrLaneRow
            key={entryKey({ item: row.item }, index)}
            row={row}
            host={group.host}
            readiness={readiness}
            needIndex={needs.findIndex((entry) => entry.item === row.item)}
            onShow={onShow}
          />
        ))}
      </ul>
    </div>
  );
}

export function PullRequestLanes({
  groups,
  readiness,
  needs,
  onShow,
}: {
  groups: PrLaneGroup[];
  readiness: ReadinessMap;
  needs: NeedsYouEntry[];
  onShow: (index: number) => void;
}) {
  return (
    <div className="flex flex-col gap-[22px]">
      {groups.map((group) => (
        <PrLane key={group.host.key} group={group} readiness={readiness} needs={needs} onShow={onShow} />
      ))}
    </div>
  );
}
