import { useId, useState } from "react";
import { PrFacts } from "../../components/PrFacts";
import { describe, entryKey, orderByAge, prTitle, type Fact, type ReadinessMap } from "../../lib/attention";
import { projectName } from "../../lib/derive";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { runKey } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { PullRequestFacts, StatusRun } from "../../lib/contract.gen";
import type { AttentionItem } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { prWait, rowFacts } from "../prRow";
import { stateOf } from "../state";
import { LaneHead } from "./LaneHead";
import { PrRowDetail } from "./PrRowDetail";
import { TONE_TEXT } from "./StateDot";

type Host = HostRecord<ContractBodies>;

const ROW_GRID =
  "grid min-h-[66px] grid-cols-[64px_minmax(0,1fr)_auto] items-start gap-x-5 gap-y-1.5 px-5 py-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 transition-colors duration-(--motion-colour) hover:bg-paper-2 group-data-open:hover:bg-transparent max-[720px]:grid-cols-[minmax(0,1fr)]";
const DETAIL = "border-t border-dashed border-rule-2 py-4 pl-[calc(20px+64px+20px)] pr-5 max-[720px]:px-5";

interface PrRow {
  item: AttentionItem;
  fixing: StatusRun | null;
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
    const own = items.filter((item) => item.daemon === host.key);
    const parked: PrRow[] = own.map((item) => {
      const pr = item.pr != null && typeof item.pr === "object" ? (item.pr as PullRequestFacts) : null;
      return { item, fixing: null, ageMs: describe(item, status.thresholds, clock).ageMs, facts: rowFacts(pr, { round: item.round, waits_on: item.waits_on }) };
    });
    const parkedUrls = new Set(own.map((item) => item.pr_url));
    const fixing: PrRow[] = status.runs
      .filter((run) => run.pr_url != null && !parkedUrls.has(run.pr_url))
      .map((run) => ({
        item: { kind: "pr_open", level: "attention", daemon: host.key, run: run.id, ticket: run.ticket, title: run.title, pr_url: run.pr_url, pr: run.pr, round: run.round },
        fixing: run,
        ageMs: run.woke_ms == null ? null : clock.now - run.woke_ms,
        facts: rowFacts(run.pr ?? null, { round: run.round }),
      }));
    const rows = [...parked, ...fixing];
    if (rows.length === 0) return [];
    return [{ host, name: projectName(status.project), rows: orderByAge(rows, (row) => row.ageMs) }];
  });
}

function PrLaneRow({ row, host, readiness }: { row: PrRow; host: Host; readiness: ReadinessMap }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const { item, facts } = row;
  const run = typeof item.run === "number" ? item.run : null;
  const state = row.fixing
    ? stateOf({ run: row.fixing, staleMs: host.status?.thresholds.heartbeat_stale_ms ?? Infinity })
    : stateOf({ attention: item, readiness: run == null ? undefined : readiness.get(runKey(host.key, run)) });
  const wait = prWait(state, item, row.ageMs);
  const word = wait.line === "" ? wait.status : `${wait.status}, ${wait.line}`;
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const label = typeof item.pr_url === "string" ? prLabel(item.pr_url) : "PR";
  const title = row.fixing ? row.fixing.title : (prTitle(item) ?? "");
  const shown = open && run != null;
  return (
    <li
      data-pr-row
      data-open={shown || undefined}
      className="group border-rule-2 data-open:bg-act-wash data-open:shadow-[inset_2px_0_0_var(--act)] [&+&]:border-t"
    >
      <div data-row className={ROW_GRID}>
        <a
          href={item.pr_url ?? undefined}
          target="_blank"
          rel="noreferrer"
          onClick={(event) => event.stopPropagation()}
          className="justify-self-start whitespace-nowrap border-b border-rule pt-[3px] text-ink hover:border-ink hover:text-ink"
        >
          {label.replace(/^PR (?=#)/, "")}
        </a>
        <button
          type="button"
          aria-expanded={run == null ? undefined : open}
          aria-controls={run == null ? undefined : detailId}
          disabled={run == null}
          aria-label={`${label}, ${ticket} ${title}, ${facts.map((fact) => fact.label).join(" · ")}, ${word}`}
          onClick={() => setOpen(!open)}
          className="flex min-h-11 min-w-0 flex-col gap-1 text-left"
        >
          <span data-pr-title className="font-read text-[15px] font-medium leading-[1.35] text-ink">
            <span className="mr-2 font-mono text-[12px] font-normal text-ink-3">{ticket}</span>
            {title}
          </span>
          <PrFacts facts={facts} form="plain" />
        </button>
        <span data-pr-state className="flex flex-col items-end gap-0.5 text-right max-[720px]:flex-row max-[720px]:flex-wrap max-[720px]:items-baseline max-[720px]:gap-x-3 max-[720px]:text-left">
          <span data-pr-wait className={`font-sans text-[14px] font-semibold leading-[1.3] ${TONE_TEXT[wait.tone]}`}>{wait.status}</span>
          {wait.line !== "" && <span data-pr-since className="whitespace-nowrap font-mono text-[12px] text-ink-3">{wait.line}</span>}
        </span>
      </div>
      {shown && (
        <div id={detailId} className={DETAIL}>
          <PrRowDetail base={host.base} project={host.status?.project ?? ""} run={run} item={item} actions={host.status?.actions === true && row.fixing == null} />
        </div>
      )}
    </li>
  );
}

/** One project's parked pull requests and those the factory is fixing, a
 *  row each that opens onto its detail. */
function PrLane({ group, readiness }: { group: PrLaneGroup; readiness: ReadinessMap }) {
  const status = group.host.status!;
  return (
    <div role="group" aria-label={group.name}>
      <LaneHead name={group.name} held={status.admission === "held"} holdNote={status.hold_note} meta={`${group.rows.length} open`} />
      <ul className="border border-rule bg-paper">
        {group.rows.map((row, index) => (
          <PrLaneRow key={entryKey({ item: row.item }, index)} row={row} host={group.host} readiness={readiness} />
        ))}
      </ul>
    </div>
  );
}

export function PullRequestLanes({ groups, readiness }: { groups: PrLaneGroup[]; readiness: ReadinessMap }) {
  return (
    <div className="flex flex-col gap-[22px]">
      {groups.map((group) => (
        <PrLane key={group.host.key} group={group} readiness={readiness} />
      ))}
    </div>
  );
}
