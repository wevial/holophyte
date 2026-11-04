import { useId, useState } from "react";
import { Link } from "@tanstack/react-router";
import { PrFacts } from "./PrFacts";
import { describe, entryKey, orderByAge, prTitle, type Fact, type ReadinessMap } from "../../lib/attention";
import { projectName } from "../../lib/derive";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { runKey } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { MergeQueue, MergeQueueEntry, PullRequestFacts } from "../../lib/contract.gen";
import type { AttentionItem } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { mergesIn } from "../mergeQueue";
import { prWait, rowFacts } from "../prRow";
import { stateOf } from "../state";
import { LaneHead } from "./LaneHead";
import { EntryDetail } from "./MergeQueueTable";
import { PrRowDetail } from "./PrRowDetail";
import { StageTab } from "./StageChip";
import { TONE_TEXT } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";

type Host = HostRecord<ContractBodies>;

const ROW_GRID =
  "grid min-h-[66px] grid-cols-[64px_minmax(0,1fr)_auto] items-start gap-x-5 gap-y-1.5 px-5 py-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 transition-colors duration-(--motion-colour) hover:bg-paper-2 group-data-open:hover:bg-transparent max-[720px]:grid-cols-[minmax(0,1fr)]";
const OPEN_ROW = "data-open:bg-act-wash data-open:shadow-[inset_2px_0_0_var(--act)]";
const DETAIL_PAD = "pl-[calc(20px+64px+20px)] pr-5 max-[720px]:px-5";
const DETAIL = `border-t border-dashed border-rule-2 py-4 ${DETAIL_PAD}`;

interface PrRow {
  item: AttentionItem;
  ageMs: number | null;
  facts: Fact[];
}

interface PrLaneGroup {
  host: Host;
  name: string;
  rows: PrRow[];
  /** The merge queue's entries in position order. */
  entries: MergeQueueEntry[];
  queueNow: number;
}

const waitsOnYou = (item: AttentionItem) =>
  item.park_kind === "question" || prWait(stateOf({ attention: item }), item, null).tone === "need";

export function prLaneGroups(hosts: Host[], items: AttentionItem[], now: number, queues: { host: Host; queue: MergeQueue }[]): PrLaneGroup[] {
  return hosts.flatMap((host) => {
    const queue = queues.find((answer) => answer.host === host)?.queue;
    const entries = queue?.configured === true ? queue.entries : [];
    const status = host.status;
    if (!status) return [];
    const clock = { now: status.now + sinceSeen(host.seen_ms, now) };
    const rows: PrRow[] = items
      .filter((item) => item.daemon === host.key && waitsOnYou(item))
      .map((item) => {
        const pr = item.pr != null && typeof item.pr === "object" ? (item.pr as PullRequestFacts) : null;
        return { item, ageMs: describe(item, status.thresholds, clock).ageMs, facts: rowFacts(pr, { round: item.round, waits_on: item.waits_on }) };
      });
    if (rows.length === 0 && entries.length === 0) return [];
    const ordered = [...entries].sort((a, b) => a.position - b.position);
    return [{ host, name: projectName(status.project), rows: orderByAge(rows, (row) => row.ageMs), entries: ordered, queueNow: queue?.now ?? 0 }];
  });
}

const runOf = (item: AttentionItem) => (typeof item.run === "number" ? item.run : null);

function rowReadiness(item: AttentionItem, host: Host, readiness: ReadinessMap) {
  const run = runOf(item);
  return run == null ? undefined : readiness.get(runKey(host.key, run));
}

function rowState(item: AttentionItem, host: Host, readiness: ReadinessMap) {
  return stateOf({ attention: item, readiness: rowReadiness(item, host, readiness) });
}

/** How many rows only wait on the maintainer's merge. */
export const readyToMergeCount = (groups: PrLaneGroup[], readiness: ReadinessMap): number =>
  groups.reduce((sum, group) => sum + group.rows.filter((row) => rowState(row.item, group.host, readiness) === "ready_to_merge").length, 0);

function PrLaneRow({ row, host, readiness }: { row: PrRow; host: Host; readiness: ReadinessMap }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const { item, facts } = row;
  const run = runOf(item);
  const state = rowState(item, host, readiness);
  const reason = rowReadiness(item, host, readiness)?.reason;
  const wait = prWait(state, item, row.ageMs, reason);
  const word = wait.line === "" ? wait.status : `${wait.status}, ${wait.line}`;
  const ticket = typeof item.ticket === "string" ? item.ticket : "";
  const label = typeof item.pr_url === "string" ? prLabel(item.pr_url) : "PR";
  const title = prTitle(item) ?? "";
  const shown = open && run != null;
  return (
    <li
      data-pr-row
      data-open={shown || undefined}
      className={`group border-rule-2 ${OPEN_ROW} [&+&]:border-t`}
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
          aria-label={`${label}, ${ticket} ${plainTitle(title)}, ${facts.map((fact) => fact.label).join(" · ")}, ${word}`}
          onClick={() => setOpen(!open)}
          className="flex min-h-11 min-w-0 flex-col gap-1 text-left"
        >
          <span data-pr-title className="font-read text-[15px] font-medium leading-[1.35] text-ink">
            <span className="mr-2 font-mono text-[12px] font-normal text-ink-3">{ticket}</span>
            <TitleText>{title}</TitleText>
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
          <PrRowDetail host={host} run={run} item={item} ready={state === "ready_to_merge"} bypass={reason === "review_bypassable"} />
        </div>
      )}
    </li>
  );
}

function QueueRow({ entry, now, host, project }: { entry: MergeQueueEntry; now: number; host: Host; project: string }) {
  const [open, setOpen] = useState(false);
  const detailId = useId();
  const building = entry.group_sha != null;
  const daemon = { project, base: host.base, actions: host.status?.actions === true };
  return (
    <li data-queue-entry data-open={open || undefined} className={`group border-rule-2 ${OPEN_ROW} [&+&]:border-t`}>
      <div className={`${ROW_GRID} cursor-pointer`} onClick={() => setOpen(!open)}>
        <a
          href={entry.pr.url}
          target="_blank"
          rel="noreferrer"
          onClick={(event) => event.stopPropagation()}
          className="justify-self-start whitespace-nowrap border-b border-rule pt-[3px] text-ink hover:border-ink hover:text-ink"
        >
          #{entry.pr.number}
        </a>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={detailId}
          className="flex min-h-11 min-w-0 flex-col gap-1 text-left"
        >
          <span className="font-read text-[15px] font-medium leading-[1.35] [overflow-wrap:anywhere] text-ink">
            <TitleText>{entry.pr.title ?? ""}</TitleText>
          </span>
        </button>
        <span className="flex flex-col items-end gap-0.5 text-right max-[720px]:flex-row max-[720px]:flex-wrap max-[720px]:items-baseline max-[720px]:gap-x-3 max-[720px]:text-left">
          <span data-queue-state className={`font-sans text-[14px] font-semibold leading-[1.3] ${building ? TONE_TEXT.wait : "text-ink-2"}`}>
            {building ? "checks running" : "queued"}
          </span>
          <span className="whitespace-nowrap">
            {entry.checks.done}/{entry.checks.total} done · merges {mergesIn(entry.estimated_s)}
          </span>
        </span>
      </div>
      {open && (
        <EntryDetail
          entry={entry}
          now={now}
          id={detailId}
          daemon={daemon}
          pad={DETAIL_PAD}
          links={<Link to="/merge-queue" search={{ project }} className="btn" data-variant="quiet">Merge queue</Link>}
        />
      )}
    </li>
  );
}

/** One project's pull requests waiting on the maintainer, a row each that
 *  opens onto its detail, then its merge queue under a tab: the head, and the
 *  rest behind a count that unfolds. */
function PrLane({ group, readiness }: { group: PrLaneGroup; readiness: ReadinessMap }) {
  const [unfolded, setUnfolded] = useState(false);
  const listId = useId();
  const status = group.host.status!;
  const { entries } = group;
  const meta = group.rows.length > 0 ? `${group.rows.length} open` : `${entries.length} queued`;
  const more = entries.length - 1;
  const shown = unfolded ? entries : entries.slice(0, 1);
  return (
    <div role="group" aria-label={group.name}>
      <LaneHead name={group.name} held={status.admission === "held"} holdNote={status.hold_note} meta={meta} />
      <ul id={listId} className="border border-rule bg-paper">
        {group.rows.map((row, index) => (
          <PrLaneRow key={entryKey({ item: row.item }, index)} row={row} host={group.host} readiness={readiness} />
        ))}
        {entries.length > 0 && (
          <StageTab
            data-queue-tab
            aria-label="Queue"
            label="Queue"
            sub={entries[0]!.estimated_s == null ? null : mergesIn(entries[0]!.estimated_s)}
            state={entries[0]!.group_sha != null ? "on_ci" : "queued"}
          >
            {more > 0 && (
              <button
                type="button"
                aria-expanded={unfolded}
                aria-controls={listId}
                onClick={() => setUnfolded(!unfolded)}
                className="mb-1 border-b border-rule font-sans text-[13px] text-ink-2 transition-colors duration-(--motion-colour) hover:border-ink hover:text-ink"
              >
                {more} more
              </button>
            )}
          </StageTab>
        )}
        {shown.map((entry) => (
          <QueueRow key={entry.pr.url} entry={entry} now={group.queueNow} host={group.host} project={group.name} />
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
