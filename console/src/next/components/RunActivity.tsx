import { useId, useMemo, useState, type ReactNode } from "react";
import type { RoundCriterion, RoundFinding, RunDetail } from "../../lib/contract.gen";
import { findingsHistory, RANK, severityOf, type Fate } from "../../lib/findings";
import { formatClock, formatSpan } from "../../lib/format";
import { criteriaGrid, PASSED, type ActivityGroup, type ActivityStep, type ActivityTone, type RunActivity as Activity } from "../activity";
import { ActivityFinding } from "./ActivityFinding";
import { CriteriaGrid } from "./CriteriaGrid";
import { CURRENT_TINT, EndFacts, endingOf } from "./EndFacts";
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

/** A timed step reads its label bold and its duration in mono, before the rest of its text. */
function StepText({ step }: { step: ActivityStep }) {
  if (step.ms == null) return <>{step.text}</>;
  const [label, ...rest] = step.text.split(" · ");
  return (
    <>
      <b className="font-semibold text-ink">{label}</b> <span className="font-mono text-[13px] text-ink-2">{step.ms < 1000 ? "<1s" : formatSpan(step.ms)}</span>
      {rest.length > 0 && ` · ${rest.join(" · ")}`}
    </>
  );
}

function Steps({ steps }: { steps: ActivityStep[] }) {
  return (
    <ol className="m-0 list-none p-0 pb-3">
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

const MARK: Record<Exclude<RoundCriterion["status"], "met">, { glyph: string; word: string; tone: string }> = {
  not_met: { glyph: "✕", word: "not met", tone: "text-bad" },
  unwitnessed: { glyph: "?", word: "unwitnessed", tone: "text-warn-ink" },
};

function Mark({ criterion }: { criterion: RoundCriterion }) {
  if (criterion.status === "met") return null;
  const mark = MARK[criterion.status];
  return (
    <span data-mark={criterion.status} className={`ml-3 inline-flex items-baseline gap-1.5 whitespace-nowrap font-semibold ${mark.tone}`}>
      <span aria-hidden="true">{mark.glyph}</span>
      {`${criterion.id} ${mark.word}`}
    </span>
  );
}

function Heading({ group, end }: { group: ActivityGroup; end: ReactNode }) {
  return (
    <>
      <span data-group-title className="min-w-0 font-sans text-[15px] leading-[1.4] text-ink-2 [overflow-wrap:anywhere]">
        <b className="font-semibold text-ink">{group.title}</b>
        {group.sub.length > 0 && ` · ${group.sub.join(" · ")}`}
        {group.kind === "round" && group.marks.map((criterion) => <Mark key={criterion.id} criterion={criterion} />)}
      </span>
      <span data-group-span className="whitespace-nowrap font-mono text-[13px] tabular-nums text-ink-2">
        {group.running ? "now" : group.ms == null ? "" : formatSpan(group.ms)}
      </span>
      {end}
    </>
  );
}

type Line = keyof typeof CONNECTOR | "none";

const LISTS_FINDINGS = new Set<ActivityGroup["kind"]>(["round", "final", "threads"]);

function groupFindings(group: ActivityGroup): RoundFinding[] {
  if (group.round == null || !LISTS_FINDINGS.has(group.kind)) return [];
  return [...group.round.findings].sort((a, b) => RANK[severityOf(a)] - RANK[severityOf(b)]);
}

function Findings({ findings, fates }: { findings: RoundFinding[]; fates: Map<RoundFinding, Fate> }) {
  return (
    <ul data-findings className="m-0 mb-3 ml-[106px] list-none border border-rule-2 bg-findings-bg p-0 max-[720px]:ml-[92px]">
      {findings.map((finding, index) => <ActivityFinding key={index} finding={finding} fate={fates.get(finding) ?? null} />)}
    </ul>
  );
}

type Current = keyof typeof CURRENT_TINT | null;

interface GroupProps {
  group: ActivityGroup;
  open: boolean;
  onToggle: () => void;
  line: Line;
  fates: Map<RoundFinding, Fate>;
  /** The current group's tone; null for every other group. */
  current: Current;
  /** What ends the current group's body: a live run's newest progress, or how the run ended. */
  tail: ReactNode;
}

function Group({ group, open, onToggle, line, fates, current, tail }: GroupProps) {
  const stepsId = useId();
  const findings = groupFindings(group);
  const folds = group.steps.length > 0 || findings.length > 0 || tail != null;
  return (
    <li
      data-group={group.kind}
      data-running={group.running ? "" : undefined}
      data-current={current ?? undefined}
      className={`relative border-b border-rule-2 ${current == null ? "" : `-ml-3 pl-3 ${CURRENT_TINT[current]}`}`}
    >
      {line !== "none" && <span aria-hidden="true" className={`absolute left-[76px] w-px bg-rule max-[720px]:left-[66px] ${current == null ? "" : "ml-3"}`} style={CONNECTOR[line]} />}
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
      {folds && open && (
        <div id={stepsId}>
          {group.steps.length > 0 && <Steps steps={group.steps} />}
          {findings.length > 0 && <Findings findings={findings} fates={fates} />}
          {tail}
        </div>
      )}
    </li>
  );
}

const groupKey = (group: ActivityGroup) => `${group.kind}:${group.at}`;

const judgedFindings = (group: ActivityGroup) =>
  group.round != null && (group.kind === "round" || (group.kind === "final" && (group.round.findings.length > 0 || PASSED.has(group.round.verdict))));

/** What became of each review finding: a later review round that no longer raises it reads fixed. A failed
 *  final check that raised no findings judged only criteria, so it says nothing of the round before it. */
function reviewFates(groups: ActivityGroup[]): Map<RoundFinding, Fate> {
  const rounds = groups.filter(judgedFindings).map((group) => group.round!);
  return new Map(findingsHistory(rounds, []).flatMap((round) => round.findings.map((each) => [each.finding as RoundFinding, each.fate] as const)));
}

interface RunActivityProps {
  activity: Activity;
  detail: RunDetail;
  base: string;
  project: string;
}

function NowDetail({ text }: { text: string }) {
  return (
    <p data-now-detail className="m-0 pb-3 pl-[106px] pr-5 font-sans text-[14px] leading-[1.45] text-ink-2 [overflow-wrap:anywhere] max-[720px]:pl-[92px]">
      {text}
    </p>
  );
}

/** The run's timeline beside its criteria by round, one row per group,
 *  each folding open to its steps and findings. The current group, the
 *  running one or an ended run's last, is tinted and ends on its newest
 *  progress or on how the run ended; a group the reader has not toggled
 *  is open exactly while it is current. */
export function RunActivity({ activity, detail, base, project }: RunActivityProps) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const { groups } = activity;
  const grid = useMemo(() => criteriaGrid(detail), [detail]);
  const fates = useMemo(() => reviewFates(groups), [groups]);
  const ending = detail.run.outcome == null ? null : endingOf(detail, activity);
  const lineOf = (index: number): Line => (groups.length < 2 ? "none" : index === 0 ? "first" : index === groups.length - 1 ? "last" : "middle");
  const currentOf = (group: ActivityGroup, index: number): Current => {
    if (index !== groups.length - 1) return null;
    if (group.running) return "live";
    return ending?.tone ?? null;
  };
  const tailOf = (group: ActivityGroup, current: Current): ReactNode => {
    if (current == null) return null;
    if (group.running) {
      const text = activity.now?.detail;
      return text == null || text === group.steps.at(-1)?.text ? null : <NowDetail text={text} />;
    }
    return ending == null ? null : <EndFacts detail={detail} ending={ending} base={base} project={project} />;
  };
  return (
    <div className="mt-4 flex flex-col gap-3 @[640px]/progress:grid @[640px]/progress:grid-cols-[minmax(0,1fr)_248px] @[640px]/progress:items-start @[640px]/progress:gap-x-6">
      {grid != null && (
        <div className="@[640px]/progress:sticky @[640px]/progress:top-0 @[640px]/progress:col-start-2 @[640px]/progress:row-start-1">
          <CriteriaGrid grid={grid} />
        </div>
      )}
      <ol aria-label="Run activity" className="m-0 list-none border-t border-rule-2 p-0 @[640px]/progress:col-start-1 @[640px]/progress:row-start-1">
        {groups.map((group, index) => {
          const key = groupKey(group);
          const current = currentOf(group, index);
          const open = chosen[key] ?? current != null;
          return (
            <Group
              key={key}
              group={group}
              open={open}
              line={lineOf(index)}
              fates={fates}
              current={current}
              tail={tailOf(group, current)}
              onToggle={() => setChosen((was) => ({ ...was, [key]: !open }))}
            />
          );
        })}
      </ol>
    </div>
  );
}
