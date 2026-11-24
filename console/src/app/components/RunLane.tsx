import { useContext } from "react";
import { Link } from "@tanstack/react-router";
import { isStale } from "../../lib/derive";
import { formatSettled } from "../../lib/format";
import { sinceSeen, type HostRecord } from "../../lib/hosts";
import { agentMs, boxPercent, groupByProject, latestEvents, phaseLabel, type ProjectGroup } from "../../lib/runs";
import type { StatusRun, WitnessSet } from "../../lib/contract.gen";
import type { ContractBodies } from "../fleet";
import { useRun } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { Hint } from "./Hint";
import { reviewRound } from "./StageChip";
import { StateDot } from "./StateDot";
import { plainTitle, TitleText } from "./TitleText";

const MINUTE = 60_000;
const HOT_PERCENT = 90;
const TRACK = ["Implement", "Verify", "Review", "Merge gate", "Merge"] as const;
const TRACK_AT: Record<string, number> = { working: 0, addressing: 0, verifying: 1, reviewing: 2, merge_gate: 3, merging: 4, squashing: 4 };

interface LaneGroup extends ProjectGroup {
  runs: StatusRun[];
  workers: number;
  witnesses: WitnessSet | null;
  staleListed: Set<number>;
}

interface RunningRow {
  run: StatusRun;
  project: string;
  base: string;
  sinceMs: number;
}

/** The project groups of the hosts that answered `/status` well. */
export function laneGroups(hosts: HostRecord<ContractBodies>[]): LaneGroup[] {
  const answered = hosts.flatMap((host) =>
    host.status && !host.contract_error ? [{ key: host.key, base: host.base, status: host.status, seen_ms: host.seen_ms, attention: host.attention }] : [],
  );
  const groups = groupByProject(answered.map((entry) => ({ ...entry, status: { ...entry.status, route_labels: undefined } })));
  return groups.map((group) => {
    const own = answered.filter((entry) => entry.base === group.base && entry.status.project === group.path);
    return {
      ...group,
      runs: own.flatMap((entry) => entry.status.runs),
      workers: own[0]!.status.workers,
      witnesses: own.find((entry) => entry.status.witnesses)?.status.witnesses ?? null,
      staleListed: new Set(own.flatMap((entry) => (entry.attention?.items ?? []).flatMap((item) => (item.kind === "stale_run" && typeof item.run === "number" ? [item.run] : [])))),
    };
  });
}

/** Every group's runs but the quiet ones its daemon already lists as Needs you items. */
export function liveRuns(groups: LaneGroup[], now: number): RunningRow[] {
  return groups.flatMap((group) => {
    const sinceMs = sinceSeen(group.seen_ms, now);
    const threshold = group.status.thresholds.heartbeat_stale_ms;
    return group.runs
      .filter((run) => !(isStale(run.heartbeat_age_ms + sinceMs, threshold) && group.staleListed.has(run.id)))
      .map((run) => ({ run, project: group.path, base: group.base, sinceMs }));
  });
}

function witnessKeys(keys: string[]): string {
  const numbers = keys.map((key) => (/^W\d+$/.test(key) ? Number(key.slice(1)) : NaN));
  const run = keys.length > 1 && numbers.every((n, index) => n === numbers[0]! + index);
  return run ? `${keys[0]}–${keys.at(-1)}` : keys.join(", ");
}

/** A line per running witness set: its story, keys and time so far. */
export function witnessLines(groups: LaneGroup[], now: number): string[] {
  return groups.flatMap(({ witnesses, seen_ms }) =>
    witnesses == null ? [] : [`Witnesses · ${witnesses.story} · ${witnessKeys(witnesses.keys)} · running ${formatSettled(witnesses.elapsed_ms + sinceSeen(seen_ms, now))}`],
  );
}

/** One dot per worker in scope, the busy ones lit. */
export function WorkerDots({ groups }: { groups: LaneGroup[] }) {
  const workers = groups.reduce((sum, group) => sum + group.workers, 0);
  const busy = Math.min(workers, groups.reduce((sum, group) => sum + group.runs.length, 0));
  const label = `${busy} of ${workers} workers busy`;
  return (
    <Hint tip={label}>
      {(trigger) => (
        <span {...trigger} role="img" aria-label={label} className="flex items-center gap-[3px]">
          {Array.from({ length: workers }, (_, index) => (
            <i key={index} data-busy={index < busy || undefined} className={`block size-[5px] rounded-full ${index < busy ? "bg-live" : "bg-rule"}`} />
          ))}
        </span>
      )}
    </Hint>
  );
}

function stageOf(run: StatusRun): { at: number | null; label: string } {
  const at = TRACK_AT[run.phase] ?? null;
  const word = at == null ? phaseLabel(run.phase) : at === 3 && run.pr_url ? "Monitoring PR" : TRACK[at];
  const round = reviewRound({ phase: run.phase, round: run.round + 1, max_rounds: run.max_rounds });
  return { at, label: round == null ? word : `${word} · ${round}` };
}

function StageTrack({ at }: { at: number | null }) {
  return (
    <span aria-hidden="true" className="flex shrink-0 gap-[3px]">
      {TRACK.map((stage, index) => (
        <i key={stage} data-step={at == null ? "later" : index < at ? "done" : index === at ? "now" : "later"} className={`block h-[2px] w-[11px] rounded-full ${at == null || index > at ? "bg-rule" : index === at ? "bg-live" : "bg-ink-3"}`} />
      ))}
    </span>
  );
}

function RunRow({ row }: { row: RunningRow }) {
  const { run, sinceMs } = row;
  const deps = useContext(PollDepsContext);
  const detail = useRun(row.base, run.id, deps);
  const latest = detail.data ? latestEvents(detail.data.events, 1)[0]?.summary : undefined;
  const activity = (latest ?? run.step ?? "").split("\n")[0]!;
  const stage = stageOf(run);
  const used = agentMs(run, sinceMs);
  const box = run.time_box_ms;
  const percent = used != null && box != null ? boxPercent(used, box) : null;
  const hot = percent != null && percent >= HOT_PERCENT;
  const minutes = used == null ? null : Math.floor(used / MINUTE);
  const spoken = minutes == null ? "" : box == null ? `, ${minutes} minutes` : `, ${minutes} of ${Math.floor(box / MINUTE)} minutes`;
  const stop = run.stop_requested ? (run.stop_action?.startsWith("abort") ? "Aborting" : "Pausing after this turn") : null;
  return (
    <li className="border-b border-rule-2 transition-colors duration-(--motion-colour) hover:bg-paper-2">
      <Link
        data-row
        {...runPage(row.project, run.id)}
        aria-label={`${run.ticket} ${plainTitle(run.title ?? "")}, ${stage.label}${spoken}`}
        className="grid grid-cols-[16px_84px_minmax(0,1fr)_270px_calc(2.5rem+0.5rem+6ch)] items-start gap-x-3 px-2.5 py-3 font-mono text-[12px] leading-[1.3] text-ink-3 no-underline @max-[680px]:grid-cols-[16px_84px_minmax(0,1fr)_calc(2.5rem+0.5rem+6ch)] @max-[420px]:grid-cols-[16px_minmax(0,1fr)_calc(2.5rem+0.5rem+6ch)]"
      >
        <span className="pt-0.5"><StateDot state="running" hideWord /></span>
        <span className="truncate pt-0.5 text-ink-2 @max-[420px]:hidden">{run.ticket}</span>
        <span className="flex min-w-0 flex-col gap-1">
          <span className="truncate font-read text-[14.5px] font-medium leading-[1.35] text-ink"><TitleText>{run.title ?? ""}</TitleText></span>
          {(stop ?? activity) !== "" && (
            <span data-activity className="flex min-w-0 items-center gap-1.5">
              <i aria-hidden="true" className="block size-1 shrink-0 rounded-full bg-live" />
              <span className="truncate">{stop ?? activity}</span>
            </span>
          )}
        </span>
        <span data-stage className="flex items-center gap-2.5 pt-1 font-sans text-[12.5px] text-ink-2 @max-[680px]:hidden">
          <StageTrack at={stage.at} />
          <span className="truncate">{stage.label}</span>
        </span>
        <span className="flex items-center justify-end gap-2 pt-1 whitespace-nowrap">
          {percent != null && (
            <span data-bar={hot ? "need" : "live"} aria-hidden="true" className="relative block h-[3px] w-10 rounded-full bg-rule-2">
              <i className={`absolute inset-y-0 left-0 rounded-full ${hot ? "bg-need" : "bg-act"}`} style={{ width: `${percent}%` }} />
            </span>
          )}
          {used != null && <span data-time className={`inline-block w-[6ch] text-right tabular-nums ${hot ? "text-need" : "text-ink-3"}`}>{formatSettled(used)}</span>}
        </span>
      </Link>
    </li>
  );
}

/** A hairline row per live run, across every project. */
export function RunningRows({ rows }: { rows: RunningRow[] }) {
  return (
    <ul aria-label="Running runs" className="@container">
      {rows.map((row) => (
        <RunRow key={`${row.base} ${row.run.id}`} row={row} />
      ))}
    </ul>
  );
}
