import { useId, useState } from "react";
import { ACTIONS_OFF } from "../../lib/actions";
import type { ScoutTask } from "../../lib/contract.gen";
import { formatSpan } from "../../lib/format";
import { useAction } from "../actions";
import { tokenWords, type ScoutGroup } from "../scout";
import type { State } from "../state";
import { StateDot } from "./StateDot";
import { Badge, Connector, GroupHead, lineOf, Steps } from "./Timeline";
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

const FACT = "whitespace-nowrap max-[719px]:before:mx-1.5 max-[719px]:before:content-['·'] max-[719px]:first:before:content-none";

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
  const dot = TASK_DOTS[task.phase];
  const stopped = task.phase === "stopped";
  const stoppable = task.phase === "queued" || task.phase === "running";
  const stopping = stoppable && stop != null && stop.sent.has(task.id);
  const last = stopping ? "stopping" : spent(task);
  return (
    <li
      data-helper={task.phase}
      className={`grid grid-cols-[16px_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1 border-b border-rule-2 px-4 py-2 last:border-b-0 ${stop ? "min-[720px]:grid-cols-[16px_minmax(0,1fr)_44px_56px_64px_64px_24px]" : "min-[720px]:grid-cols-[16px_minmax(0,1fr)_44px_56px_64px_64px]"}`}
    >
      <span className="self-start pt-[3px]"><StateDot state={dot.state} label={dot.label} hideWord /></span>
      <span data-question className={`min-w-0 font-sans text-[14px] leading-[1.45] [overflow-wrap:anywhere] ${stopped ? "text-ink-3 line-through" : "text-ink"}`}>
        {task.question}
      </span>
      <span className="col-start-2 flex flex-wrap font-mono text-[12px] leading-[1.4] text-ink-2 min-[720px]:contents">
        <span className={FACT}>
          <span data-mode={task.mode} className={`inline-block px-1 text-[11px] font-semibold leading-[15px] ${task.mode === "run" ? "border border-transparent bg-[color-mix(in_oklab,var(--wait)_22%,transparent)] text-wait" : "border border-rule text-ink-2"}`}>
            {task.mode}
          </span>
        </span>
        <span data-route className={FACT}>{task.route ?? DASH}</span>
        <span data-took className={`${FACT} tabular-nums min-[720px]:text-right`}>{took(task, now)}</span>
        <span data-tokens className={`${FACT} tabular-nums min-[720px]:text-right ${stopped || stopping ? "text-wait" : ""} ${last === "" ? "max-[719px]:hidden" : ""}`}>{last}</span>
        {stop && (stoppable && !stopping ? <StopBranch task={task} stop={stop} /> : <span aria-hidden="true" className="max-[719px]:hidden" />)}
      </span>
    </li>
  );
}

function Helpers({ tasks, now, stop }: { tasks: ScoutTask[]; now: number; stop: RowStop | null }) {
  return (
    <ol aria-label="Helpers" className="relative z-[1] m-0 mb-3 ml-[106px] list-none border border-rule-2 bg-paper-2 p-0 max-[720px]:ml-0">
      {tasks.map((task, index) => <HelperRow key={`${task.id}:${index}`} task={task} now={now} stop={stop} />)}
    </ol>
  );
}

function Group({ group, open, onToggle, line, now, stop }: {
  group: ScoutGroup; open: boolean; onToggle: () => void; line: ReturnType<typeof lineOf>; now: number; stop: RowStop | null;
}) {
  const bodyId = useId();
  const helpers = group.kind === "wave" || group.kind === "pass";
  const folds = helpers ? group.tasks.length > 0 : !group.running && group.steps.length > 0;
  const shown = folds ? open : group.running && group.steps.length > 0;
  return (
    <li data-group={group.kind} data-running={group.running ? "" : undefined} className="relative border-b border-rule-2">
      <Connector line={line} />
      <GroupHead at={group.at} badge={<Badge tone={group.tone} running={group.running} />} folds={folds} open={open} controls={bodyId} onToggle={onToggle}>
        <span data-group-title className="min-w-0 font-sans text-[15px] leading-[1.4] text-ink-2 [overflow-wrap:anywhere]">
          <b className="font-semibold text-ink">{group.title}</b>
          {group.sub.length > 0 && ` · ${group.sub.join(" · ")}`}
        </span>
        <span data-group-span className="whitespace-nowrap font-mono text-[13px] tabular-nums text-ink-2">
          {group.running ? "now" : group.ms == null ? "" : formatSpan(group.ms)}
        </span>
      </GroupHead>
      {shown && (
        <div id={bodyId}>
          {helpers ? <Helpers tasks={group.tasks} now={now} stop={stop} /> : <Steps steps={group.steps} />}
        </div>
      )}
    </li>
  );
}

const troubled = (group: ScoutGroup) => group.tasks.some((task) => task.phase === "stopped" || task.phase === "failed");

/** The scout's timeline, one row per group. A group the reader has not
 *  toggled is open while it runs, holds a stopped or failed helper, or is the newest pass. With `stop`
 *  each queued or running helper ends in Stop this branch. */
export function ScoutActivity({ groups, now, stop }: { groups: ScoutGroup[]; now: number; stop: HelperStop | null }) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const [sent, setSent] = useState<ReadonlySet<number>>(new Set());
  const rowStop = stop == null ? null : { ...stop, sent, onSent: (task: number) => setSent((was) => new Set(was).add(task)) };
  const newestPass = groups.findLast((group) => group.kind === "pass")?.key;
  return (
    <ol aria-label="Scout activity" className="m-0 mt-4 list-none border-t border-rule-2 p-0">
      {groups.map((group, index) => {
        const open = chosen[group.key] ?? (group.running || troubled(group) || group.key === newestPass);
        return (
          <Group
            key={group.key}
            group={group}
            open={open}
            line={lineOf(index, groups.length)}
            now={now}
            stop={rowStop}
            onToggle={() => setChosen((was) => ({ ...was, [group.key]: !open }))}
          />
        );
      })}
    </ol>
  );
}
