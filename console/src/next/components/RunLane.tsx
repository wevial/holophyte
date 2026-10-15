import { useId, useState } from "react";
import { TicketLink } from "../../components/TicketLink";
import { isStale } from "../../lib/derive";
import { formatDuration, formatSettled } from "../../lib/format";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { agentMs, boxPercent, groupByProject, phaseLabel, type ProjectGroup } from "../../lib/runs";
import { prLabel } from "../../lib/shipped";
import type { Queued, Run } from "../../lib/types";
import type { ContractBodies } from "../fleet";
import { LaneHead } from "./LaneHead";
import { StageChip } from "./StageChip";
import { StateDot } from "./StateDot";

const MINUTE = 60_000;
const HOT_PERCENT = 90;

const ROW_GRID =
  "grid grid-cols-[110px_84px_minmax(0,1fr)_150px] items-center gap-x-5 gap-y-1.5 px-5 py-3.5 font-mono text-[12px] leading-[1.3] text-ink-3 max-[820px]:grid-cols-[auto_auto_minmax(0,1fr)]";
const TITLE_CELL = "min-w-0 font-sans text-[13px] leading-[1.45] max-[820px]:col-span-full max-[820px]:row-start-2";
const TIME_CELL = "grid gap-[7px] whitespace-nowrap text-right max-[820px]:col-start-3 max-[820px]:row-start-1";

/** The project groups of the hosts that answered `/status` well. The
 *  lanes read no route labels, whose contract type the classic one lacks. */
export function laneGroups(hosts: HostRecord<ContractBodies>[]): ProjectGroup[] {
  return groupByProject(
    hosts.flatMap((host) =>
      host.status && !host.contract_error
        ? [{ base: host.base, status: { ...host.status, route_labels: undefined }, seen_ms: host.seen_ms }]
        : [],
    ),
  );
}

/** "A went quiet. It is up in Needs you.", "A and B went quiet. They are up in Needs you." */
function quietSentence(keys: string[]): string {
  const named = keys.length === 1 ? keys[0] : `${keys.slice(0, -1).join(", ")} and ${keys.at(-1)}`;
  return `${named} went quiet. ${keys.length === 1 ? "It is" : "They are"} up in Needs you.`;
}

function RunLaneRow({ run, base, sinceMs }: { run: Run; base: string; sinceMs: number }) {
  const stage = phaseLabel(run.phase);
  const used = agentMs(run, sinceMs);
  const box = run.time_box_ms;
  const percent = used != null && box != null ? boxPercent(used, box) : null;
  const tone = percent != null && percent >= HOT_PERCENT ? "need" : "live";
  const minutes = used == null ? null : Math.floor(used / MINUTE);
  const spoken = minutes == null ? "" : box == null ? `, ${minutes} minutes` : `, ${minutes} of ${Math.floor(box / MINUTE)} minutes`;
  const title = run.title ?? "";
  const stop = run.stop_requested ? `${run.stop_action?.startsWith("abort") ? "Abort" : "Pause"} requested: ${run.stop_requested}` : null;
  return (
    <li className="border-rule-2 [&+&]:border-t">
      <a
        href={`/#run=${run.id}&daemon=${encodeURIComponent(base)}`}
        aria-label={`${run.ticket} ${title}, ${stage}${spoken}`}
        className={`${ROW_GRID} min-h-[66px] no-underline transition-colors duration-(--motion-colour) hover:bg-paper-2`}
      >
        <span className="min-w-0">
          <StageChip run={{ phase: run.phase, round: (run.round ?? 0) + 1 }} />
        </span>
        <span className="whitespace-nowrap text-ink-2">{run.ticket}</span>
        <span className={TITLE_CELL}>
          <span className="mb-0.5 block text-[14.5px] font-medium text-ink">{title}</span>
          {stop && <span className="block truncate text-ink-3">{stop}</span>}
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
      </a>
    </li>
  );
}

function QueuedLaneRow({ entry, held, sinceMs }: { entry: Queued; held: boolean; sinceMs: number }) {
  const reason = entry.reason ?? "";
  const shown = [reason.split("\n")[0], held ? "· held" : ""].filter(Boolean).join(" ");
  return (
    <li data-queued={entry.ticket} className={`${ROW_GRID} min-h-[54px] border-rule-2 [&+&]:border-t`}>
      <span>
        <StateDot state="queued" />
      </span>
      <span className="whitespace-nowrap text-ink-2">
        <TicketLink ticket={entry.ticket} ticket_url={entry.ticket_url} />
      </span>
      <span className={TITLE_CELL}>
        <span className="mb-0.5 block text-[14.5px] font-medium text-ink">{entry.title ?? ""}</span>
        {shown && (
          <span title={reason || undefined} className="block truncate text-ink-3">
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
 *  drawn, and its queue behind a count that unfolds. */
function RunLane({ group, now }: { group: ProjectGroup; now: number }) {
  const [open, setOpen] = useState(false);
  const queueId = useId();
  const sinceMs = sinceSeen(group.seen_ms, now);
  const threshold = group.status.thresholds.heartbeat_stale_ms;
  const quiet = (run: Run) => isStale(run.heartbeat_age_ms + sinceMs, threshold);
  const live = group.runs.filter((run) => !quiet(run));
  const stale = group.runs.filter(quiet);
  const queued = group.queued ?? [];
  const held = group.status.admission === "held";
  return (
    <div role="group" aria-label={group.name}>
      <LaneHead name={group.name} held={held} holdNote={group.status.hold_note} meta={`${live.length} running`}>
        {stale.length > 0 ? (
          <span>· {quietSentence(stale.map((run) => run.ticket))}</span>
        ) : (
          live.length === 0 && <span>· Nothing running.</span>
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
      {live.length > 0 && (
        <ul className="border border-rule bg-paper">
          {live.map((run) => (
            <RunLaneRow key={run.id} run={run} base={group.base} sinceMs={sinceMs} />
          ))}
        </ul>
      )}
      {queued.length > 0 && (
        <ul id={queueId} hidden={!open} aria-label={`${group.name} queued`} className="mt-2 border border-rule bg-paper">
          {open && queued.map((entry) => <QueuedLaneRow key={entry.ticket} entry={entry} held={held} sinceMs={sinceMs} />)}
        </ul>
      )}
    </div>
  );
}

/** A lane per project group, in the groups' order. */
export function RunningLanes({ groups, now }: { groups: ProjectGroup[]; now: number }) {
  return (
    <div className="flex flex-col gap-[22px]">
      {groups.map((group) => (
        <RunLane key={`${group.base} ${group.path}`} group={group} now={now} />
      ))}
    </div>
  );
}
