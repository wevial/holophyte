import { useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Timeline,
  TimelineConnector,
  TimelineDescription,
  TimelineItem,
  TimelineTitle,
} from "@ui-sdk/react/timeline";
import type { RoundCriterion, RoundFinding, RunDetail } from "../../lib/contract.gen";
import { formatSpan } from "../../lib/format";
import { findingsHistory, RANK, severityOf, type Fate } from "../../lib/findings";
import { PASSED, type ActivityGroup, type RunActivity as Activity } from "../activity";
import { ActivityFinding } from "./ActivityFinding";
import { EndFacts, endingOf, type Ending } from "./EndFacts";
import { Badge, GroupHead, Steps } from "./Timeline";

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

function Heading({ group, current }: { group: ActivityGroup; current: boolean }) {
  return (
    <>
      <TimelineTitle render={<span />} data-group-title className="min-w-0 font-sans text-[15px] leading-[22px] text-ink-2 [overflow-wrap:anywhere]">
        <b className={`font-medium ${current ? "text-ink" : "text-ink-2"}`}>{group.title}</b>
        {group.sub.length > 0 && ` · ${group.sub.join(" · ")}`}
        {group.kind === "round" && group.marks.map((criterion) => <Mark key={criterion.id} criterion={criterion} />)}
      </TimelineTitle>
      <TimelineDescription render={<span />} data-group-span className="flex h-[22px] items-center whitespace-nowrap font-mono text-[13px] tabular-nums text-ink-2">
        {group.running ? "now" : group.ms == null ? "" : formatSpan(group.ms)}
      </TimelineDescription>
    </>
  );
}

const LISTS_FINDINGS = new Set<ActivityGroup["kind"]>(["round", "final", "threads", "covering_review"]);

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

type Current = Ending["tone"] | "live" | null;

const TABBABLE =
  'a[href],area[href],button,input:not([type="hidden"]),select,textarea,summary,iframe,[tabindex],[contenteditable]:not([contenteditable="false"])';
const savedTabIndex = new WeakMap<HTMLElement, string | null>();

function syncSubtreeTabbable(root: HTMLElement, tabbable: boolean) {
  if (tabbable) {
    for (const el of root.querySelectorAll<HTMLElement>('[tabindex="-1"]')) {
      if (!savedTabIndex.has(el)) continue;
      const previous = savedTabIndex.get(el)!;
      savedTabIndex.delete(el);
      if (previous == null) el.removeAttribute("tabindex");
      else el.setAttribute("tabindex", previous);
    }
    return;
  }
  for (const el of root.querySelectorAll<HTMLElement>(TABBABLE)) {
    if (el.getAttribute("tabindex") === "-1") continue;
    savedTabIndex.set(el, el.getAttribute("tabindex"));
    el.setAttribute("tabindex", "-1");
  }
}

interface GroupProps {
  group: ActivityGroup;
  value: string;
  open: boolean;
  tabbable: boolean;
  onFocusItem: () => void;
  onToggle: () => void;
  fates: Map<RoundFinding, Fate>;
  /** The current group's tone; null for every other group. */
  current: Current;
  /** What ends the current group's body: a live run's newest progress, or how the run ended. */
  tail: ReactNode;
}

function Group({ group, value, open, tabbable, onFocusItem, onToggle, fates, current, tail }: GroupProps) {
  const stepsId = useId();
  const itemRef = useRef<HTMLDivElement>(null);
  const findings = groupFindings(group);
  const folds = group.steps.length > 0 || findings.length > 0 || tail != null;
  useLayoutEffect(() => {
    const item = itemRef.current;
    if (item == null) return;
    syncSubtreeTabbable(item, tabbable);
    if (tabbable || typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver((records) => {
      if (records.some((record) => record.type === "childList" || record.target !== item)) syncSubtreeTabbable(item, false);
    });
    observer.observe(item, { childList: true, subtree: true, attributes: true, attributeFilter: ["tabindex", "href"] });
    return () => observer.disconnect();
  }, [tabbable, open, folds]);
  return (
    <TimelineItem
      ref={itemRef}
      value={value}
      id={group.anchor ?? (group.number == null ? undefined : `round-${group.number}`)}
      data-group={group.kind}
      data-running={group.running ? "" : undefined}
      data-current={current ?? undefined}
      onFocus={(event) => {
        onFocusItem();
        if (event.target !== event.currentTarget && event.currentTarget.getAttribute("tabindex") !== "0") {
          // Timeline's roving focus listens only to non-bubbling focus on the item itself.
          event.currentTarget.dispatchEvent(new FocusEvent("focus"));
          event.currentTarget.dispatchEvent(new FocusEvent("blur"));
        }
      }}
      className="group/item relative scroll-mt-4 border-b border-rule-2"
    >
      <TimelineConnector className="pointer-events-none absolute top-[27px] -bottom-[28px] left-[76px] w-px bg-rule group-data-last/item:hidden max-[720px]:left-[66px]" />
      <GroupHead at={group.at} badge={<Badge tone={group.tone} running={group.running} icon={group.kind === "pickup" ? "pickup" : undefined} />} folds={folds} open={open} controls={stepsId} onToggle={onToggle}>
        <Heading group={group} current={current != null} />
      </GroupHead>
      {folds && open && (
        <div id={stepsId}>
          {group.steps.length > 0 && <Steps steps={group.steps} />}
          {findings.length > 0 && <Findings findings={findings} fates={fates} />}
          {tail}
        </div>
      )}
    </TimelineItem>
  );
}

const groupKey = (group: ActivityGroup) => {
  if (group.anchor != null) return `${group.kind}:${group.anchor}`;
  if ((group.kind === "round" || group.kind === "final") && group.number != null) return `round:${group.number}`;
  return `${group.kind}:${group.at}`;
};

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

/** The run's timeline, one row per group,
 *  each folding open to its steps and findings. The current group, the
 *  running one or an ended run's last, has its title in full ink and ends on its newest
 *  progress or on how the run ended; a group the reader has not toggled
 *  is open exactly while it is current. */
export function RunActivity({ activity, detail, base, project }: RunActivityProps) {
  const [chosen, setChosen] = useState<Record<string, boolean>>({});
  const [tabStop, setTabStop] = useState<string | null>(null);
  const { groups } = activity;
  const keys = groups.map(groupKey);
  const stopKey = tabStop != null && keys.includes(tabStop) ? tabStop : (keys[0] ?? null);
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
    <div className="mt-4">
      <Timeline aria-label="Run activity" className="border-t border-rule-2">
        {groups.map((group, index) => {
          const key = keys[index]!;
          const current = currentOf(group, index);
          const open = chosen[key] ?? current != null;
          return (
            <Group
              key={key}
              value={key}
              group={group}
              open={open}
              tabbable={key === stopKey}
              fates={fates}
              current={current}
              tail={tailOf(group, current)}
              onFocusItem={() => setTabStop(key)}
              onToggle={() => setChosen((was) => ({ ...was, [key]: !open }))}
            />
          );
        })}
      </Timeline>
    </div>
  );
}
