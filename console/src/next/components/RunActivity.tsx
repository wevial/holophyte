import { useId, useRef, useState, type ReactNode } from "react";
import { formatClock, formatSpan } from "../../lib/format";
import type { ActivityGroup, ActivityNow, ActivityStep, ActivityTone, RunActivity as Activity } from "../activity";
import { FoldChevron } from "./icons";
import { StateDot } from "./StateDot";

const ROW = "grid grid-cols-[48px_30px_minmax(0,1fr)_auto_14px] items-center gap-x-3.5 max-[720px]:grid-cols-[42px_30px_minmax(0,1fr)_auto_14px] max-[720px]:gap-x-2.5";
const BADGE_CENTRE = 27;
const CONNECTOR = { first: { top: BADGE_CENTRE, bottom: 0 }, middle: { top: 0, bottom: 0 }, last: { top: 0, height: BADGE_CENTRE } };

const BADGE_TONE: Record<ActivityTone, string> = {
  ok: "bg-live-wash text-live",
  warn: "bg-warn-wash text-warn-ink",
  bad: "bg-bad-wash text-bad",
  wait: "bg-wait-wash text-wait",
  act: "bg-act-wash text-act",
  live: "bg-live-wash text-live",
  neutral: "bg-paper-2 text-ink-3",
};

const ICON_PATHS: Partial<Record<ActivityTone | "pickup", string>> = {
  ok: "M3.5 8.4 6.6 11.5 12.5 5",
  warn: "M3.2 8a4.8 4.8 0 1 0 1.4-3.4M3.2 2.6v2.6h2.6",
  bad: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  wait: "M8 1.5a6.5 6.5 0 1 1 0 13a6.5 6.5 0 1 1 0-13zM8 4.8V8l2.3 1.6",
  act: "M10.6 2.6l2.8 2.8-7.6 7.6H3v-2.8zM9 4.2l2.8 2.8",
  pickup: "M4 2.5v6h8.5M9.5 5.5l3 3-3 3",
  neutral: "M8 5.5a2.5 2.5 0 1 1 0 5a2.5 2.5 0 1 1 0-5z",
};

function Badge({ group }: { group: ActivityGroup }) {
  const path = ICON_PATHS[group.kind === "pickup" ? "pickup" : group.tone];
  return (
    <span data-badge={group.tone} className={`relative z-[1] flex size-[30px] items-center justify-center rounded-md ${BADGE_TONE[group.tone]}`}>
      {group.running ? (
        <StateDot state="running" hideWord tone="live" />
      ) : (
        <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d={path} />
        </svg>
      )}
    </span>
  );
}

const stepSpan = (ms: number) => (ms < 1000 ? "<1s" : formatSpan(ms));

/** A timed step reads its label bold and its duration in mono, before the rest of its text. */
function StepText({ step }: { step: ActivityStep }) {
  if (step.ms == null) return <>{step.text}</>;
  const [label, ...rest] = step.text.split(" · ");
  return (
    <>
      <b className="font-semibold text-ink">{label}</b> <span className="font-mono text-[13px] text-ink-2">{stepSpan(step.ms)}</span>
      {rest.length > 0 && ` · ${rest.join(" · ")}`}
    </>
  );
}

function Steps({ id, steps }: { id: string; steps: ActivityStep[] }) {
  return (
    <ol id={id} className="m-0 list-none p-0 pb-3">
      {steps.map((step, index) => (
        <li key={`${step.at}:${index}`} data-step className={`${ROW} !items-baseline py-1`}>
          <time dateTime={new Date(step.at).toISOString()} className="font-mono text-[13px] text-ink-3">
            {formatClock(step.at)}
          </time>
          <span data-step-text className={`col-span-3 col-start-3 font-sans text-[14px] leading-[1.45] [overflow-wrap:anywhere] ${step.quiet ? "text-ink-2" : "text-ink"}`}>
            <StepText step={step} />
          </span>
        </li>
      ))}
    </ol>
  );
}

function Heading({ group, end }: { group: ActivityGroup; end: ReactNode }) {
  return (
    <>
      <span data-group-title className="min-w-0 font-sans text-[15px] leading-[1.4] text-ink-2 [overflow-wrap:anywhere]">
        <b className="font-semibold text-ink">{group.title}</b>
        {group.sub.length > 0 && ` · ${group.sub.join(" · ")}`}
      </span>
      <span data-group-span className="whitespace-nowrap font-mono text-[13px] tabular-nums text-ink-2">
        {group.running ? "now" : group.ms == null ? "" : formatSpan(group.ms)}
      </span>
      {end}
    </>
  );
}

type Line = keyof typeof CONNECTOR | "none";

function Group({ group, open, onToggle, line }: { group: ActivityGroup; open: boolean; onToggle: () => void; line: Line }) {
  const stepsId = useId();
  const folds = !group.running && group.steps.length > 0;
  const shown = group.running || (folds && open);
  return (
    <li data-group={group.kind} data-running={group.running ? "" : undefined} className="relative border-b border-rule-2">
      {line !== "none" && <span aria-hidden="true" className="absolute left-[76px] w-px bg-rule max-[720px]:left-[66px]" style={CONNECTOR[line]} />}
      <div className={`${ROW} min-h-[54px] py-3`}>
        <time dateTime={new Date(group.at).toISOString()} className="font-mono text-[13px] text-ink-2">
          {formatClock(group.at)}
        </time>
        <Badge group={group} />
        {folds ? (
          <button
            type="button"
            aria-expanded={open}
            aria-controls={stepsId}
            onClick={onToggle}
            className="group col-span-3 col-start-3 grid cursor-pointer grid-cols-subgrid items-center border-0 bg-transparent p-0 text-left"
          >
            <Heading group={group} end={<FoldChevron open={open} />} />
          </button>
        ) : (
          <div className="col-span-3 col-start-3 grid grid-cols-subgrid items-center">
            <Heading group={group} end={null} />
          </div>
        )}
      </div>
      {shown && group.steps.length > 0 && <Steps id={stepsId} steps={group.steps} />}
    </li>
  );
}

const groupKey = (group: ActivityGroup) => `${group.kind}:${group.at}`;

function openAtFirst(group: ActivityGroup, index: number, groups: ActivityGroup[]) {
  return index === groups.length - 1 || group.tone === "bad" || group.tone === "act" || groups.findIndex((each) => each.marks.length > 0) === index;
}

function NowBar({ now, needsYou }: { now: ActivityNow; needsYou: boolean }) {
  return (
    <section
      aria-label="Now"
      className="sticky top-0 z-10 mt-4 border-b-2 border-ink bg-live-wash py-3.5 pl-5 pr-5 shadow-[inset_4px_0_0_var(--live)] max-[720px]:pl-4"
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span aria-hidden="true" className="self-center">
          <StateDot state="running" hideWord tone="live" />
        </span>
        <b className="font-sans text-[16px] font-semibold leading-[1.3] text-ink">{now.what}</b>
        <span className="ml-auto font-mono text-[13px] text-ink-2">since {formatClock(now.since)}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1.5 pl-[26px]">
        {now.detail != null && <span data-now-detail className="font-sans text-[14px] leading-[1.45] text-ink-2 [overflow-wrap:anywhere]">{now.detail}</span>}
        {!needsYou && <span className="bg-live/15 px-2 py-0.5 font-sans text-[13px] font-semibold text-ok-ink">Nothing needs you</span>}
      </div>
    </section>
  );
}

/** The run's timeline: a live run's Now bar, then one row per group, each
 *  folding open to its steps. A reader's fold choice holds across polls;
 *  a group seen for the first time opens when it is the last, gone wrong,
 *  acting, or the first with marks. */
export function RunActivity({ activity, needsYou }: { activity: Activity; needsYou: boolean }) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const firstSeen = useRef(new Map<string, boolean>());
  const { groups } = activity;
  groups.forEach((group, index) => {
    const key = groupKey(group);
    if (!firstSeen.current.has(key)) firstSeen.current.set(key, openAtFirst(group, index, groups));
  });
  const lineOf = (index: number): Line => (groups.length < 2 ? "none" : index === 0 ? "first" : index === groups.length - 1 ? "last" : "middle");
  return (
    <>
      {activity.now != null && <NowBar now={activity.now} needsYou={needsYou} />}
      <ol aria-label="Run activity" className="m-0 mt-3 list-none border-t border-rule-2 p-0">
        {groups.map((group, index) => {
          const key = groupKey(group);
          const open = chosen[key] ?? firstSeen.current.get(key) ?? false;
          return <Group key={key} group={group} open={open} line={lineOf(index)} onToggle={() => setChosen((was) => ({ ...was, [key]: !open }))} />;
        })}
      </ol>
    </>
  );
}
