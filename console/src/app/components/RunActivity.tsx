import { useId, useMemo, useState, type ReactNode } from "react";
import type { RoundCriterion, RoundFinding, RunDetail } from "../../lib/contract.gen";
import { formatSpan } from "../../lib/format";
import { findingsHistory, RANK, severityOf, type Fate } from "../../lib/findings";
import { criteriaGrid, PASSED, type ActivityGroup, type RunActivity as Activity } from "../activity";
import { ActivityFinding } from "./ActivityFinding";
import { CriteriaGrid } from "./CriteriaGrid";
import { CURRENT_TINT, EndFacts, endingOf } from "./EndFacts";
import { Badge, Connector, GroupHead, lineOf, Steps, type Line } from "./Timeline";

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

function Heading({ group }: { group: ActivityGroup }) {
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
    </>
  );
}

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
      <Connector line={line} shifted={current != null} />
      <GroupHead at={group.at} badge={<Badge tone={group.tone} running={group.running} icon={group.kind === "pickup" ? "pickup" : undefined} />} folds={folds} open={open} controls={stepsId} onToggle={onToggle}>
        <Heading group={group} />
      </GroupHead>
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
  const currentOf = (group: ActivityGroup, index: number): Current => (index !== groups.length - 1 ? null : group.running ? "live" : (ending?.tone ?? null));
  const tailOf = (group: ActivityGroup, current: Current): ReactNode => {
    if (current == null) return null;
    if (group.running) {
      const text = activity.now?.detail;
      return text == null || text === group.steps.at(-1)?.text ? null : <NowDetail text={text} />;
    }
    return ending == null ? null : <EndFacts detail={detail} ending={ending} base={base} project={project} />;
  };
  return (
    <div className={`mt-4 flex flex-col gap-3 ${grid == null ? "" : "@[640px]/progress:grid @[640px]/progress:grid-cols-[minmax(0,1fr)_248px] @[640px]/progress:items-start @[640px]/progress:gap-x-6"}`}>
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
              line={lineOf(index, groups.length)}
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
