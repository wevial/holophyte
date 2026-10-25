import { useId, useState } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import type { ScoutTask } from "../../lib/contract.gen";
import { formatSpan } from "../../lib/format";
import { useAction } from "../actions";
import { tokenWords, type ScoutGroup } from "../scout";
import type { State } from "../state";
import { FoldChevron } from "./icons";
import { StateDot } from "./StateDot";
import { Badge, GroupHead } from "./Timeline";
import { TitleText } from "./TitleText";
import { useToast } from "./Toast";

const DASH = "–";

const TASK_DOTS: Record<ScoutTask["phase"], { state: State; label: string }> = {
  queued: { state: "queued", label: "waiting" },
  running: { state: "running", label: "running" },
  done: { state: "ready_to_merge", label: "back" },
  failed: { state: "failed", label: "failed" },
  stopped: { state: "abandoned", label: "stopped" },
};

function took(task: ScoutTask, now: number): string {
  if (task.phase === "running" && task.started_ms != null) return formatSpan(now - task.started_ms);
  if (task.phase === "queued" || task.duration_ms == null) return DASH;
  return formatSpan(task.duration_ms);
}

function spent(task: ScoutTask): string {
  if (task.phase === "stopped") return "stopped";
  if (task.phase === "running") return "…";
  return task.phase === "queued" || task.tokens == null ? "" : tokenWords(task.tokens);
}

/** Where a swarming scout's helpers are stopped one at a time. */
export interface HelperStop {
  base: string;
  scout: number;
  actions: boolean;
}

/** A helper stop with the tasks whose stop the daemon took, kept above the rows a fold unmounts. */
interface RowStop extends HelperStop {
  sent: ReadonlySet<number>;
  onSent: (task: number) => void;
}

const STOP_BRANCH = "Stop this branch";

/** ✕ for one queued or running helper: `POST /actions/scout-stop` with its task, no confirm; a refusal says its detail in the toast. */
function StopBranch({ task, stop }: { task: ScoutTask; stop: RowStop }) {
  const action = useAction();
  const toast = useToast();
  const press = async () => {
    const result = await action.mutateAsync({
      label: STOP_BRANCH, base: stop.base, route: "/actions/scout-stop", body: { scout: stop.scout, task: task.id },
      said: `Stop sent to task ${task.id} of scout ${stop.scout}.`,
    });
    if (result.ok) stop.onSent(task.id);
    else toast(result.detail);
  };
  return (
    <button type="button" aria-label={STOP_BRANCH} title={stop.actions ? STOP_BRANCH : ACTIONS_OFF} disabled={!stop.actions || action.isPending}
      onClick={() => void press()}
      className="self-center px-1 font-mono text-[12px] leading-none text-ink-3 hover:text-bad disabled:cursor-not-allowed disabled:opacity-40 max-[719px]:ml-auto max-[719px]:pl-2 min-[720px]:justify-self-end">
      ✕
    </button>
  );
}

function HelperRow({ task, now, stop }: { task: ScoutTask; now: number; stop: RowStop | null }) {
  const [open, setOpen] = useState(false);
  const promptId = useId();
  const dot = TASK_DOTS[task.phase];
  const stopped = task.phase === "stopped";
  const stoppable = task.phase === "queued" || task.phase === "running";
  const stopping = stoppable && stop != null && stop.sent.has(task.id);
  const last = stopping ? "stopping" : spent(task);
  const reason = task.phase === "failed" ? task.reason : null;
  return (
    <li data-helper={task.phase} className="border-b border-rule-2 last:border-b-0">
      <div className="flex items-start">
        <button type="button" aria-expanded={open} aria-controls={promptId} onClick={() => setOpen(!open)}
          className="group grid min-w-0 flex-1 cursor-pointer grid-cols-[16px_minmax(0,1fr)_14px] items-start gap-x-2 border-0 bg-transparent px-4 py-2 text-left">
          <span className="pt-[3px]"><StateDot state={dot.state} label={dot.label} hideWord /></span>
          <span className="flex min-w-0 flex-col gap-1">
            <span data-task-title className={`font-read text-[15px] font-medium leading-[1.4] [overflow-wrap:anywhere] ${stopped ? "text-ink-3 line-through" : "text-ink"}`}>
              {task.title}
            </span>
            {reason != null && <span data-task-reason className="font-mono text-[12px] leading-[1.4] text-bad-text [overflow-wrap:anywhere]">{reason}</span>}
            <span data-task-meta className="font-mono text-[12px] leading-[1.4] text-ink-3">
              <span data-mode={task.mode} className={`inline-block px-1 text-[11px] font-semibold leading-[15px] ${task.mode === "run" ? "border border-transparent bg-[color-mix(in_oklab,var(--wait)_22%,transparent)] text-wait" : "border border-rule text-ink-2"}`}>
                {task.mode}
              </span>
              {" · "}
              <span data-route>{task.route ?? DASH}</span>
              {" · "}
              <span data-took className="tabular-nums">{took(task, now)}</span>
              {last !== "" && " · "}
              <span data-tokens className={`tabular-nums ${stopped || stopping ? "text-wait" : ""}`}>{last}</span>
            </span>
          </span>
          <span className="pt-1"><FoldChevron open={open} /></span>
        </button>
        {stop && stoppable && !stopping && <span className="pr-4 pt-2"><StopBranch task={task} stop={stop} /></span>}
      </div>
      {open && (
        <p id={promptId} data-question className="m-0 max-w-[70ch] px-4 pb-4 pl-10 font-read text-[14px] leading-[1.55] text-ink-2 [overflow-wrap:anywhere]">
          <TitleText>{task.question}</TitleText>
        </p>
      )}
    </li>
  );
}

function Helpers({ tasks, now, stop }: { tasks: ScoutTask[]; now: number; stop: RowStop | null }) {
  return (
    <ol aria-label="Helpers" className="m-0 mb-4 ml-[106px] list-none border border-rule-2 bg-paper-2 p-0 max-[720px]:mx-0">
      {tasks.map((task, index) => <HelperRow key={`${task.id}:${index}`} task={task} now={now} stop={stop} />)}
    </ol>
  );
}

function Group({ group, open, onToggle, now, stop }: {
  group: ScoutGroup; open: boolean; onToggle: () => void; now: number; stop: RowStop | null;
}) {
  const bodyId = useId();
  const folds = (group.kind === "wave" || group.kind === "pass") && group.tasks.length > 0;
  return (
    <li data-group={group.kind} data-running={group.running ? "" : undefined} className="border-b border-rule-2 px-4 last:border-b-0 max-[720px]:px-2">
      <GroupHead at={group.at} badge={<Badge tone={group.tone} running={group.running} />} folds={folds} open={open} controls={bodyId} onToggle={onToggle}>
        <span data-group-title className="min-w-0 font-sans text-[15px] leading-[1.4] text-ink-2 [overflow-wrap:anywhere]">
          <b className="font-semibold text-ink">{group.title}</b>
          {group.sub.length > 0 && ` · ${group.sub.join(" · ")}`}
          {group.failed > 0 && <>{" · "}<span data-failed className="font-medium text-bad-text">{group.failed} failed</span></>}
        </span>
        <span data-group-span className="whitespace-nowrap font-mono text-[13px] tabular-nums text-ink-2">
          {group.running ? "now" : group.ms == null ? "" : formatSpan(group.ms)}
        </span>
      </GroupHead>
      {folds && open && (
        <div id={bodyId}>
          <Helpers tasks={group.tasks} now={now} stop={stop} />
        </div>
      )}
    </li>
  );
}

/** The scout's Progress, one row per group. Only a wave or pass holding tasks folds, closed until the reader opens it
 *  unless it runs. With `stop` each queued or running helper ends in Stop this branch. */
export function ScoutActivity({ groups, now, stop }: { groups: ScoutGroup[]; now: number; stop: HelperStop | null }) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const [sent, setSent] = useState<ReadonlySet<number>>(new Set());
  const rowStop = stop == null ? null : { ...stop, sent, onSent: (task: number) => setSent((was) => new Set(was).add(task)) };
  if (groups.length === 0) return null;
  return (
    <ol aria-label="Scout activity" className="m-0 mt-2 list-none border border-rule bg-paper p-0">
      {groups.map((group) => {
        const open = chosen[group.key] ?? group.running;
        return (
          <Group
            key={group.key}
            group={group}
            open={open}
            now={now}
            stop={rowStop}
            onToggle={() => setChosen((was) => ({ ...was, [group.key]: !open }))}
          />
        );
      })}
    </ol>
  );
}
