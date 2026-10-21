import { Link } from "@tanstack/react-router";
import { useId, useState } from "react";
import { TicketLink } from "../../components/TicketLink";
import { ageOf, orderByAge, prTitle } from "../../lib/attention";
import { isStale } from "../../lib/derive";
import { formatDuration, formatSettled } from "../../lib/format";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { agentMs, boxPercent, groupByProject, phaseLabel, type ProjectGroup } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { StatusRun, WitnessSet } from "../../lib/contract.gen";
import type { AttentionItem, Queued } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { prWait } from "../prRow";
import { runPage } from "../run";
import { stateOf } from "../state";
import { LaneHead } from "./LaneHead";
import { reviewRound, StageChip } from "./StageChip";
import { StateDot } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";

const MINUTE = 60_000;
const HOT_PERCENT = 90;

const ROW_GRID =
  "grid grid-cols-[110px_84px_minmax(0,1fr)_150px] items-center gap-x-5 gap-y-1.5 px-5 py-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 max-[820px]:grid-cols-[auto_auto_minmax(0,1fr)] @max-[640px]/ov-col:grid-cols-[auto_auto_minmax(0,1fr)]";
const TITLE_CELL = "min-w-0 font-read text-[15px] font-medium leading-[1.35] max-[820px]:col-span-full @max-[640px]/ov-col:col-span-full max-[820px]:row-start-2 @max-[640px]/ov-col:row-start-2";
const FIXING_LINE = "-mt-2.5 pb-3.5 pl-[254px] pr-5 font-mono text-[12px] text-ink-3 max-[820px]:pl-5 @max-[640px]/ov-col:pl-5";
const TIME_CELL = "grid gap-[7px] whitespace-nowrap text-right max-[820px]:col-start-3 @max-[640px]/ov-col:col-start-3 max-[820px]:row-start-1 @max-[640px]/ov-col:row-start-1";

const bareNumber = (url: string) => prLabel(url).replace(/^PR (?=#)/, "");

interface OnCiRow {
  item: AttentionItem;
  ageMs: number | null;
  line: string;
}

export interface LaneGroup extends ProjectGroup {
  runs: StatusRun[];
  workers: number;
  onCi: OnCiRow[];
  witnesses: WitnessSet | null;
}

function onCiRows(items: AttentionItem[], key: string, clockNow: number): OnCiRow[] {
  return items.flatMap((item) => {
    if (item.daemon !== key) return [];
    const ageMs = ageOf(item, clockNow);
    const wait = prWait(stateOf({ attention: item }), item, ageMs);
    return wait.status === "On CI" ? [{ item, ageMs, line: wait.line }] : [];
  });
}

/** The project groups of the hosts that answered `/status` well. The
 *  lanes read no route labels, whose contract type the classic one lacks. */
export function laneGroups(hosts: HostRecord<ContractBodies>[], items: AttentionItem[], now: number): LaneGroup[] {
  const answered = hosts.flatMap((host) =>
    host.status && !host.contract_error ? [{ key: host.key, base: host.base, status: host.status, seen_ms: host.seen_ms }] : [],
  );
  const groups = groupByProject(answered.map((entry) => ({ ...entry, status: { ...entry.status, route_labels: undefined } })));
  return groups.map((group) => {
    const own = answered.filter((entry) => entry.base === group.base && entry.status.project === group.path);
    const onCi = own.flatMap((entry) => onCiRows(items, entry.key, entry.status.now + sinceSeen(entry.seen_ms, now)));
    return {
      ...group,
      runs: own.flatMap((entry) => entry.status.runs),
      workers: own[0]!.status.workers,
      onCi: orderByAge(onCi, (row) => row.ageMs),
      witnesses: own.find((entry) => entry.status.witnesses)?.status.witnesses ?? null,
    };
  });
}

function witnessKeys(keys: string[]): string {
  const numbers = keys.map((key) => (/^W\d+$/.test(key) ? Number(key.slice(1)) : NaN));
  const run = keys.length > 1 && numbers.every((n, index) => n === numbers[0]! + index);
  return run ? `${keys[0]}–${keys.at(-1)}` : keys.join(", ");
}

function quietSentence(keys: string[]): string {
  const named = keys.length === 1 ? keys[0] : `${keys.slice(0, -1).join(", ")} and ${keys.at(-1)}`;
  return `${named} went quiet. ${keys.length === 1 ? "It is" : "They are"} up in Waiting on you.`;
}

function RunLaneRow({ run, project, sinceMs }: { run: StatusRun; project: string; sinceMs: number }) {
  const stage = phaseLabel(run.phase);
  const used = agentMs(run, sinceMs);
  const box = run.time_box_ms;
  const percent = used != null && box != null ? boxPercent(used, box) : null;
  const tone = percent != null && percent >= HOT_PERCENT ? "need" : "live";
  const minutes = used == null ? null : Math.floor(used / MINUTE);
  const spoken = minutes == null ? "" : box == null ? `, ${minutes} minutes` : `, ${minutes} of ${Math.floor(box / MINUTE)} minutes`;
  const title = run.title ?? "";
  const chip = { phase: run.phase, round: run.round + 1, max_rounds: run.max_rounds };
  const round = reviewRound(chip);
  const stop = run.stop_requested ? `${run.stop_action?.startsWith("abort") ? "Abort" : "Pause"} requested: ${run.stop_requested}` : null;
  return (
    <li className="border-rule-2 transition-colors duration-(--motion-colour) hover:bg-paper-2 [&+&]:border-t">
      <Link
        data-row
        {...runPage(project, run.id)}
        aria-label={`${run.ticket} ${plainTitle(title)}, ${stage}${round == null ? "" : `, ${round}`}${spoken}`}
        className={`${ROW_GRID} min-h-[66px] no-underline`}
      >
        <span className="min-w-0">
          <StageChip run={chip} />
        </span>
        <span className="whitespace-nowrap text-ink-2">{run.ticket}</span>
        <span className={TITLE_CELL}>
          <span className="mb-0.5 block text-ink"><TitleText>{title}</TitleText></span>
          {stop && <span className="block truncate font-sans text-[13px] font-normal leading-[1.45] text-ink-3">{stop}</span>}
        </span>
        <span className={TIME_CELL}>
          {used != null && (
            <span className={tone === "need" ? "text-need" : "text-ink-3"}>
              {formatSettled(used)}
              {box != null && ` / ${formatSettled(box)}`}
            </span>
          )}
          {percent != null && (
            <span data-bar={tone} aria-hidden="true" className="relative block h-[3px] bg-rule-2">
              <i className={`absolute inset-y-0 left-0 ${tone === "need" ? "bg-need" : "bg-live"}`} style={{ width: `${percent}%` }} />
            </span>
          )}
        </span>
      </Link>
      {run.pr_url && (
        <p data-fixing className={FIXING_LINE}>
          fixing{" "}
          <a className="border-b border-rule text-ink-2 hover:border-ink hover:text-ink" href={run.pr_url} target="_blank" rel="noreferrer">
            {bareNumber(run.pr_url)}
          </a>
        </p>
      )}
    </li>
  );
}

function OnCiLaneRow({ row }: { row: OnCiRow }) {
  const { item } = row;
  return (
    <li data-row data-on-ci className={`${ROW_GRID} min-h-[54px] border-rule-2 [&+&]:border-t`}>
      <span>
        {item.pr_url && (
          <a className="border-b border-rule text-ink hover:border-ink" href={item.pr_url} target="_blank" rel="noreferrer">
            {bareNumber(item.pr_url)}
          </a>
        )}
      </span>
      <span className="whitespace-nowrap text-ink-2">{typeof item.ticket === "string" ? item.ticket : ""}</span>
      <span className={TITLE_CELL}>
        <span className="block text-ink">{prTitle(item) ?? ""}</span>
      </span>
      <span className={TIME_CELL}>{row.line}</span>
    </li>
  );
}

function QueuedLaneRow({ entry, held, sinceMs }: { entry: Queued; held: boolean; sinceMs: number }) {
  const reason = entry.reason ?? "";
  const shown = [reason.split("\n")[0], held ? "· held" : ""].filter(Boolean).join(" ");
  return (
    <li data-row data-queued={entry.ticket} className={`${ROW_GRID} min-h-[54px] border-rule-2 [&+&]:border-t`}>
      <span>
        <StateDot state="queued" />
      </span>
      <span className="whitespace-nowrap text-ink-2">
        <TicketLink ticket={entry.ticket} ticket_url={entry.ticket_url} />
      </span>
      <span className={TITLE_CELL}>
        <span className="mb-0.5 block text-ink"><TitleText>{entry.title ?? ""}</TitleText></span>
        {shown && (
          <span title={reason || undefined} className="block truncate font-sans text-[13px] font-normal leading-[1.45] text-ink-3">
            {shown}
          </span>
        )}
      </span>
      <span className={TIME_CELL}>
        {entry.pr_url && (
          <a className="text-ink-2 hover:text-ink" href={entry.pr_url} target="_blank" rel="noopener noreferrer">
            {prLabel(entry.pr_url)}
          </a>
        )}
        <span>waiting {formatDuration(entry.waiting_ms + sinceMs)}</span>
      </span>
    </li>
  );
}

/** One project's live runs, the runs that went quiet named instead of
 *  drawn, its pull requests on CI, and its queue behind a count that unfolds. */
function RunLane({ group, now }: { group: LaneGroup; now: number }) {
  const [open, setOpen] = useState(false);
  const queueId = useId();
  const sinceMs = sinceSeen(group.seen_ms, now);
  const threshold = group.status.thresholds.heartbeat_stale_ms;
  const quiet = (run: StatusRun) => isStale(run.heartbeat_age_ms + sinceMs, threshold);
  const live = group.runs.filter((run) => !quiet(run));
  const stale = group.runs.filter(quiet);
  const queued = group.queued ?? [];
  const held = group.status.admission === "held";
  return (
    <div role="group" aria-label={group.name}>
      <LaneHead name={group.name} held={held} holdNote={group.status.hold_note} meta={`${group.runs.length} of ${group.workers} workers busy`}>
        {stale.length > 0 ? (
          <span>· {quietSentence(stale.map((run) => run.ticket))}</span>
        ) : live.length > 0 ? null : group.onCi.length > 0 ? (
          <span>· {group.onCi.length} on CI</span>
        ) : (
          !group.witnesses && <span>· Nothing running.</span>
        )}
        {queued.length > 0 && (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={queueId}
            onClick={() => setOpen(!open)}
            className="ml-auto border-b border-rule text-ink-2 transition-colors duration-(--motion-colour) hover:border-ink hover:text-ink"
          >
            {queued.length} queued
          </button>
        )}
      </LaneHead>
      {(live.length > 0 || group.onCi.length > 0) && (
        <ul aria-label={`${group.name} running`} className="border border-rule bg-paper">
          {live.map((run) => (
            <RunLaneRow key={run.id} run={run} project={group.path} sinceMs={sinceMs} />
          ))}
          {group.onCi.length > 0 && (
            <li data-on-ci-divider className={`px-5 pb-1.5 pt-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 ${live.length > 0 ? "border-t border-rule-2" : ""}`}>
              On CI
            </li>
          )}
          {group.onCi.map((row, index) => (
            <OnCiLaneRow key={row.item.pr_url ?? index} row={row} />
          ))}
        </ul>
      )}
      {group.witnesses && (
        <p data-witnesses className="mt-2 font-mono text-[12px] text-ink-3">
          Witnesses · {group.witnesses.story} · {witnessKeys(group.witnesses.keys)} · running{" "}
          {formatSettled(group.witnesses.elapsed_ms + sinceMs)}
        </p>
      )}
      {queued.length > 0 && (
        <ul id={queueId} hidden={!open} aria-label={`${group.name} queued`} className="mt-2 border border-rule bg-paper">
          {open && queued.map((entry) => <QueuedLaneRow key={entry.ticket} entry={entry} held={held} sinceMs={sinceMs} />)}
        </ul>
      )}
    </div>
  );
}

export function hasWork(group: LaneGroup): boolean {
  return group.runs.length > 0 || group.witnesses != null || group.onCi.length > 0 || (group.queued ?? []).length > 0 || group.status.admission === "held";
}

export function RunningLanes({ groups, now }: { groups: LaneGroup[]; now: number }) {
  return (
    <div className="flex flex-col gap-[22px]">
      {groups.filter(hasWork).map((group) => (
        <RunLane key={`${group.base} ${group.path}`} group={group} now={now} />
      ))}
    </div>
  );
}
