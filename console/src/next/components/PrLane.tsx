import { useContext, useId, useState } from "react";
import { AskAction } from "../../components/AskAction";
import { PrFacts } from "../../components/PrFacts";
import { PullRequestDetail } from "../../components/PullRequestDetail";
import { describe, entryKey, orderByAge, prTitle, waitingOnCi, type Fact, type NeedsYouEntry, type ReadinessMap } from "../../lib/attention";
import { projectName } from "../../lib/derive";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { runKey } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { AttentionItem } from "../../lib/types";
import { classicHref } from "../../lib/view";
import type { ContractBodies } from "../fleet";
import { PollDepsContext, useConsolePeers } from "../scope";
import { STATES, stateOf, type State } from "../state";
import { LaneHead } from "./LaneHead";
import { StateDot } from "./StateDot";

type Host = HostRecord<ContractBodies>;

const ROW_GRID =
  "grid min-h-[66px] grid-cols-[70px_84px_minmax(0,1fr)_120px_128px] items-center gap-x-5 gap-y-1.5 px-5 py-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 transition-colors duration-(--motion-colour) hover:bg-paper-2 max-[820px]:grid-cols-[minmax(0,1fr)] @max-[720px]/ov-col:grid-cols-[minmax(0,1fr)]";
const ROW_BUTTON =
  "col-span-4 grid grid-cols-subgrid items-center gap-x-5 gap-y-1.5 text-left max-[820px]:col-span-1 @max-[720px]/ov-col:col-span-1 max-[820px]:grid-cols-[auto_auto_minmax(0,1fr)] @max-[720px]/ov-col:grid-cols-[auto_auto_minmax(0,1fr)]";
const UNDER_TITLE = "max-[820px]:col-span-full @max-[720px]/ov-col:col-span-full";

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

/** One lane per host that answered `/status`, holding the `pr_open` items
 *  its daemon sent, longest waiting first. */
export function prLaneGroups(hosts: Host[], items: AttentionItem[], now: number): PrLaneGroup[] {
  return hosts.flatMap((host) => {
    const status = host.status;
    if (!status) return [];
    const clock = { now: status.now + sinceSeen(host.seen_ms, now) };
    const rows = items
      .filter((item) => item.daemon === host.key)
      .map((item) => {
        const description = describe(item, status.thresholds, clock);
        return { item, ageMs: description.ageMs, facts: description.facts ?? [] };
      });
    return [{ host, name: projectName(status.project), rows: orderByAge(rows, (row) => row.ageMs) }];
  });
}

/** What follows the state word: the checks a `ci` park waits on, else a
 *  plain park's first fact that is not `ok`. */
function stateDetail(state: State, item: AttentionItem, facts: Fact[]): string | null {
  if (state === "on_ci") return waitingOnCi(item);
  if (state === "parked") return facts.find((fact) => fact.tone !== "ok")?.label ?? null;
  return null;
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
  const deps = useContext(PollDepsContext);
  const { polls, now } = useConsolePeers();
  const { item, facts } = row;
  const run = typeof item.run === "number" ? item.run : null;
  const state = stateOf({ attention: item, readiness: run == null ? undefined : readiness.get(runKey(host.key, run)) });
  const detail = stateDetail(state, item, facts);
  const word = detail == null ? STATES[state].word : `${STATES[state].word} · ${detail}`;
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const label = typeof item.pr_url === "string" ? prLabel(item.pr_url) : "PR";
  const title = prTitle(item) ?? "";
  return (
    <li data-pr-row className="border-rule-2 [&+&]:border-t">
      <div className={ROW_GRID}>
        <button
          type="button"
          aria-expanded={run == null ? undefined : open}
          aria-controls={run == null ? undefined : detailId}
          disabled={run == null}
          aria-label={`${label}, ${ticket} ${title}, ${facts.map((fact) => fact.label).join(" · ")}, ${word}`}
          onClick={() => setOpen(!open)}
          className={ROW_BUTTON}
        >
          <span className="whitespace-nowrap text-ink">{label}</span>
          <span className="whitespace-nowrap text-ink-2">{ticket}</span>
          <span className={`min-w-0 font-sans text-[14.5px] font-medium leading-[1.45] text-ink ${UNDER_TITLE}`}>{title}</span>
          <span className={`min-w-0 ${UNDER_TITLE}`}>
            <PrFacts facts={facts} form="line" />
          </span>
        </button>
        <span className="flex flex-col items-end gap-1 text-right max-[820px]:flex-row @max-[720px]/ov-col:flex-row max-[820px]:flex-wrap @max-[720px]/ov-col:flex-wrap max-[820px]:items-baseline @max-[720px]/ov-col:items-baseline max-[820px]:gap-x-3 @max-[720px]/ov-col:gap-x-3 max-[820px]:text-left @max-[720px]/ov-col:text-left">
          <span data-pr-state>
            <StateDot state={state} />
            {detail != null && <span className="whitespace-nowrap"> · {detail}</span>}
          </span>
          {needIndex >= 0 && (
            <button
              type="button"
              aria-label={`Show ${ticket} in Needs you`}
              onClick={() => onShow(needIndex)}
              className="font-semibold text-need hover:underline"
            >
              needs you
            </button>
          )}
        </span>
      </div>
      {open && run != null && (
        <div id={detailId} className="flex flex-col gap-3 border-t border-dashed border-rule-2 px-5 py-4">
          <PullRequestDetail
            base={host.base}
            id={run}
            item={item}
            now={host.status ? host.status.now + sinceSeen(host.seen_ms, now) : now}
            polls={polls}
            deps={deps}
            runPage={classicHref()}
          />
          {host.status?.actions === true && <AskAction base={host.base} runId={run} prUrl={item.pr_url} polls={polls} deps={deps} />}
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
      <LaneHead name={group.name} held={status.admission === "held"} holdNote={status.hold_note} meta={`${group.rows.length} open`}>
        {group.rows.length === 0 && <span>· No pull requests open.</span>}
      </LaneHead>
      {group.rows.length > 0 && (
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
      )}
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
