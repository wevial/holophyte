import { useId, useState } from "react";
import type { ScoutTask } from "../../lib/contract.gen";
import { formatSpan } from "../../lib/format";
import { tokenWords, type ScoutGroup } from "../scout";
import type { State } from "../state";
import { StateDot } from "./StateDot";
import { Badge, Connector, GroupHead, lineOf, Steps } from "./Timeline";

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

function HelperRow({ task, now }: { task: ScoutTask; now: number }) {
  const dot = TASK_DOTS[task.phase];
  const stopped = task.phase === "stopped";
  return (
    <li
      data-helper={task.phase}
      className="grid grid-cols-[16px_minmax(0,1fr)] items-baseline gap-x-3 gap-y-1 border-b border-rule-2 px-4 py-2.5 last:border-b-0 min-[720px]:grid-cols-[16px_minmax(0,1fr)_44px_56px_64px_64px]"
    >
      <span className="self-start pt-[3px]"><StateDot state={dot.state} label={dot.label} hideWord /></span>
      <span data-question className={`min-w-0 font-sans text-[14px] leading-[1.45] [overflow-wrap:anywhere] ${stopped ? "text-ink-3 line-through" : "text-ink"}`}>
        {task.question}
      </span>
      <span className="col-start-2 flex flex-wrap font-mono text-[12px] leading-[1.4] text-ink-2 min-[720px]:contents">
        <span className={FACT}>
          <span data-mode={task.mode} className={`inline-block px-1 text-[11px] font-semibold leading-[15px] ${task.mode === "run" ? "border border-wait-wash bg-wait-wash text-wait" : "border border-rule text-ink-2"}`}>
            {task.mode}
          </span>
        </span>
        <span data-route className={FACT}>{task.route ?? DASH}</span>
        <span data-took className={`${FACT} tabular-nums min-[720px]:text-right`}>{took(task, now)}</span>
        <span data-tokens className={`${FACT} tabular-nums min-[720px]:text-right ${stopped ? "text-wait" : ""} ${spent(task) === "" ? "max-[719px]:hidden" : ""}`}>{spent(task)}</span>
      </span>
    </li>
  );
}

function Helpers({ tasks, now }: { tasks: ScoutTask[]; now: number }) {
  return (
    <ol aria-label="Helpers" className="relative z-[1] m-0 mb-3 ml-[106px] list-none border border-rule-2 bg-findings-bg p-0 max-[720px]:ml-0">
      {tasks.map((task, index) => <HelperRow key={`${task.id}:${index}`} task={task} now={now} />)}
    </ol>
  );
}

function Group({ group, open, onToggle, line, now }: { group: ScoutGroup; open: boolean; onToggle: () => void; line: ReturnType<typeof lineOf>; now: number }) {
  const bodyId = useId();
  const folds = group.kind === "wave" ? group.tasks.length > 0 : !group.running && group.steps.length > 0;
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
      {folds && open && (
        <div id={bodyId}>
          {group.kind === "wave" ? <Helpers tasks={group.tasks} now={now} /> : <Steps steps={group.steps} />}
        </div>
      )}
    </li>
  );
}

const troubled = (group: ScoutGroup) => group.tasks.some((task) => task.phase === "stopped" || task.phase === "failed");

/** The scout's timeline, one row per group. A group the reader has not
 *  toggled is open while it runs or holds a stopped or failed helper. */
export function ScoutActivity({ groups, now }: { groups: ScoutGroup[]; now: number }) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  return (
    <ol aria-label="Scout activity" className="m-0 mt-4 list-none border-t border-rule-2 p-0">
      {groups.map((group, index) => {
        const open = chosen[group.key] ?? (group.running || troubled(group));
        return (
          <Group
            key={group.key}
            group={group}
            open={open}
            line={lineOf(index, groups.length)}
            now={now}
            onToggle={() => setChosen((was) => ({ ...was, [group.key]: !open }))}
          />
        );
      })}
    </ol>
  );
}
