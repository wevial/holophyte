import { useContext, useState, type ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { Listbox, ListboxItem, ListboxItemIndicator } from "@ui-sdk/react/listbox";
import type { Flow } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { BarChart, Change, LineChart, Sparkline, type TrendNumber } from "../components/FlowCharts";
import { Hint } from "../components/Hint";
import { DropIcon } from "../components/icons";
import { dayRangeLabel, ToolPopover } from "../components/ListFilters";
import { PageHeader } from "../components/PageHeader";
import { Picker } from "../components/Picker";
import {
  BY_DESIGN,
  flowNumbers,
  flowSpan,
  flowWeekly,
  formatHours,
  formatPerMerge,
  formatReverted,
  formatRounds,
  oneDecimal,
  WAIT_WORDS,
  weeklyTips,
  type FlowBucket,
  type FlowNumbers,
  type FlowWeekly,
  sameWindow,
} from "../flow";
import { dayStart, validDay } from "../history";
import { storeScope } from "../projects";
import { useFlows, type FlowWindow } from "../queries";
import { PollDepsContext, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";

const HEADING = "font-sans text-[13px] font-semibold text-ink";
const H2 = `flex h-[30px] items-center border-b border-hair ${HEADING}`;
const INSIDE_MIN = 8;
const RAISED = "rounded-[10px] bg-raised shadow-[inset_0_0_0_1px_var(--hair)]";
const MENU = "rounded-[9px] bg-raised shadow-[0_0_0_1px_var(--hair-2),0_10px_28px_light-dark(rgba(29,24,43,.12),rgba(0,0,0,.35))]";

const RANGES = [
  { value: "7d", label: "Last 7 days", days: 7 },
  { value: "30d", label: "Last 30 days", days: 30 },
  { value: "90d", label: "Last 90 days", days: 90 },
] as const;
type FlowRange = (typeof RANGES)[number]["value"];
const DEFAULT_RANGE: FlowRange = "30d";
const ALL = "";
const DAYS_CAP = 182;
const DAILY_CAP = 31;

export interface FlowSearch {
  range?: FlowRange;
  from?: string;
  to?: string;
}

function dayEnd(day: string): Date {
  const start = dayStart(day);
  return new Date(start.getFullYear(), start.getMonth(), start.getDate() + 1);
}

function wholeDays(from: string, to: string): number {
  return Math.round((dayEnd(to).getTime() - dayStart(from).getTime()) / 86_400_000);
}

function endedRange(from: string | undefined, to: string | undefined): boolean {
  if (from == null || to == null) return false;
  const days = wholeDays(from, to);
  return days >= 1 && days <= DAYS_CAP && dayEnd(to).getTime() <= Date.now();
}

function lastEndedDay(): string {
  const day = new Date();
  day.setDate(day.getDate() - 1);
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

export function validateFlowSearch(search: Record<string, unknown>): FlowSearch {
  const from = validDay(search.from);
  const to = validDay(search.to);
  if (endedRange(from, to)) return { range: undefined, from, to };
  const range = RANGES.find((each) => each.value === search.range && each.value !== DEFAULT_RANGE);
  return { range: range?.value, from: undefined, to: undefined };
}

function presetOf(range: string | undefined) {
  return RANGES.find((each) => each.value === range) ?? RANGES.find((each) => each.value === DEFAULT_RANGE)!;
}

function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone;
}

function bucketOf(days: number): FlowBucket {
  return days <= DAILY_CAP ? "day" : "week";
}

function flowWindow({ range, from, to }: FlowSearch): FlowWindow {
  if (from == null || to == null) {
    const { days } = presetOf(range);
    return { days, bucket: bucketOf(days), tz: localZone() };
  }
  const days = wholeDays(from, to);
  return { days, bucket: bucketOf(days), tz: localZone(), until: dayEnd(to).getTime() };
}

const SPLIT = [
  { part: "working", name: "Working", fill: "bg-live" },
  { part: "waiting", name: "Waiting to merge", fill: "bg-wait" },
  { part: "operator", name: "Waiting on the operator", fill: "bg-need" },
] as const;

function share(part: number, whole: number): number {
  return whole > 0 ? (100 * part) / whole : 0;
}

interface Trend {
  number: TrendNumber;
  values: (number | null)[];
}

function Figure({ label, children, tone = "text-ink", trend, bucket, note }: { label: string; children: ReactNode; tone?: string; trend?: Trend; bucket?: FlowBucket; note?: string }) {
  return (
    <div className={`flex min-w-0 flex-col gap-2 px-3.5 py-3 transition-colors duration-(--motion-colour) hover:bg-hover ${RAISED}`}>
      <dt className="font-sans text-[12px] text-ink-3">{label}</dt>
      <dd className="flex flex-col gap-2">
        <span className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <span data-number={label} className={`font-sans text-[30px] font-[240] font-stretch-120% leading-none tracking-[-0.02em] ${tone}`}>{children}</span>
          {trend && <Change number={trend.number} values={trend.values} bucket={bucket} />}
        </span>
        {trend && <Sparkline values={trend.values} />}
      </dd>
      {note && <dd className="font-sans text-[12px] leading-[1.5] text-ink-3">{note}</dd>}
    </div>
  );
}

function Numbers({ numbers, weekly, bucket }: { numbers: FlowNumbers; weekly: FlowWeekly; bucket: FlowBucket }) {
  return (
    <dl aria-label="Flow numbers" className="grid grid-cols-5 gap-3 max-[960px]:grid-cols-2">
      <Figure label="Merged" bucket={bucket} trend={{ number: "merged", values: weekly.merged }}>{numbers.merged}</Figure>
      <Figure label="Waiting" tone="text-wait" bucket={bucket} trend={{ number: "waiting", values: weekly.waitingShare }}>
        {numbers.waitingShare == null ? "–" : `${numbers.waitingShare}%`}
      </Figure>
      <Figure label="Rounds per merge" bucket={bucket} trend={{ number: "rounds", values: weekly.rounds }}>{formatRounds(numbers.rounds)}</Figure>
      <Figure label="Interventions per merge" bucket={bucket} trend={{ number: "interventions", values: weekly.interventionsPerMerge }}>
        {formatPerMerge(numbers.interventionsPerMerge)}
      </Figure>
      <Figure label="Reverted" tone="text-live" note="of these merges, within 14 days">{formatReverted(numbers.reverted)}</Figure>
    </dl>
  );
}

const percent = (value: number) => `${Math.round(value)}%`;
const tenths = (value: number) => value.toFixed(1);

function WeekByWeek({ weekly, allProjects, bucket }: { weekly: FlowWeekly; allProjects: boolean; bucket: FlowBucket }) {
  const { starts } = weekly;
  const tips = weeklyTips(weekly, allProjects);
  const per = `per ${bucket}`;
  return (
    <section aria-labelledby="flow-weeks" className="flex flex-col gap-3">
      <h2 id="flow-weeks" className={H2}>{bucket === "day" ? "Day by day" : "Week by week"}</h2>
      <div className="grid grid-cols-[repeat(auto-fit,minmax(min(100%,420px),1fr))] gap-3">
        <BarChart title="Merges" note={per} starts={starts} bucket={bucket} values={weekly.merged} format={oneDecimal} tip={tips.merged} />
        <LineChart title="Time spent waiting" note="share of run time" starts={starts} bucket={bucket} values={weekly.waitingShare} top={100} format={percent} tip={tips.waiting} />
        {allProjects ? (
          <LineChart title="Interventions" note={per} starts={starts} bucket={bucket} values={weekly.interventions} format={oneDecimal} tip={tips.interventions} />
        ) : (
          <LineChart title="Review rounds per merge" note="median" starts={starts} bucket={bucket} values={weekly.rounds} format={oneDecimal} tip={tips.rounds} />
        )}
        <LineChart title="Interventions per merge" note={per} starts={starts} bucket={bucket} values={weekly.interventionsPerMerge} format={tenths} tip={tips.perMerge} />
      </div>
    </section>
  );
}

function TimeSplit({ numbers }: { numbers: FlowNumbers }) {
  return (
    <section aria-labelledby="flow-split" className="flex flex-col gap-3">
      <header className="flex h-[30px] items-center gap-2.5 border-b border-hair">
        <h2 id="flow-split" className={HEADING}>Where run time went</h2>
        <span className="font-mono text-[12px] text-ink-3">{Math.round(numbers.total)} run-hours</span>
      </header>
      <div data-split className="flex h-12 bg-paper-2">
        {SPLIT.map(({ part, name, fill }) => {
          const width = share(numbers.hours[part], numbers.total);
          if (width <= 0) return null;
          return (
            <Hint key={part} tip={name} describe>
              {(trigger) => (
                <div
                  {...trigger}
                  data-part={part}
                  style={{ width: `${width}%` }}
                  className={`flex items-center overflow-hidden px-2 not-first:border-l-2 not-first:border-canvas ${fill}`}
                >
                  {width >= INSIDE_MIN && <span className="font-mono text-[12px] font-semibold text-canvas">{Math.round(width)}%</span>}
                </div>
              )}
            </Hint>
          );
        })}
      </div>
      <ul aria-label="Run time" className="flex flex-wrap gap-x-10 gap-y-2">
        {SPLIT.map(({ part, name, fill }) => (
          <li key={part} className="flex items-center gap-2 font-sans text-[13px] font-semibold text-ink">
            <span aria-hidden="true" className={`size-2.5 ${fill}`} />
            {name}
            <span className="font-mono font-normal text-ink-2">{formatHours(numbers.hours[part])}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function BiggestWaits({ waits }: { waits: FlowNumbers["waits"] }) {
  const top = waits[0]?.hours ?? 0;
  return (
    <section aria-labelledby="flow-waits" className="flex flex-col gap-3">
      <h2 id="flow-waits" className={H2}>Biggest waits</h2>
      <ol aria-label="Biggest waits" className="flex flex-col gap-3.5">
        {waits.map(({ group, hours }, index) => (
          <li key={group} data-wait={group} className="flex flex-col gap-1.5">
            <span className="flex items-center justify-between gap-3 font-sans text-[13px] text-ink">
              <span className="flex items-center gap-2">
                {WAIT_WORDS[group] ?? group}
                {BY_DESIGN.has(group) && <span className="inline-flex h-5 items-center rounded-[5px] bg-selected px-[7px] font-sans text-[11.5px] font-semibold leading-none text-ink-2">by design</span>}
              </span>
              <span className="font-mono text-[12px] text-ink-2">{formatHours(hours)}</span>
            </span>
            <span aria-hidden="true" className="block h-1 rounded-full bg-paper-2">
              <span className={`block h-full rounded-full ${index === 0 ? "bg-need" : "bg-wait"}`} style={{ width: `${share(hours, top)}%` }} />
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

interface ProjectFlow {
  name: string;
  numbers: FlowNumbers;
}

function ProjectsTable({ projects, scope, pick }: { projects: ProjectFlow[]; scope: string | undefined; pick: (name: string) => void }) {
  const cell = "h-[34px] border-b border-hair pr-2.5 font-mono text-[12.5px] text-ink-2";
  const head = "h-[30px] border-b border-hair pr-2.5 font-sans text-[12px] font-medium text-ink-3";
  return (
    <section aria-labelledby="flow-projects" className="flex flex-col gap-3">
      <h2 id="flow-projects" className={H2}>Projects side by side</h2>
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="text-left">
            <th scope="col" className={head}>Project</th>
            <th scope="col" className={`${head} text-right`}>Merged</th>
            <th scope="col" className={`${head} text-right`}>Waiting</th>
            <th scope="col" className={`${head} text-right`}>Rounds</th>
            <th scope="col" className={`${head} text-right`}>Reverted</th>
          </tr>
        </thead>
        <tbody>
          {projects.map(({ name, numbers }) => (
            <tr key={name} aria-current={name === scope ? "true" : undefined} className="hover:bg-hover aria-[current]:not-hover:bg-selected">
              <th scope="row" className="h-[34px] border-b border-hair pr-2.5 text-left">
                <button type="button" onClick={() => pick(name)} className="cursor-pointer font-sans text-[13px] font-medium text-ink hover:text-act">{name}</button>
              </th>
              <td className={`${cell} text-right`}>{numbers.merged}</td>
              <td className={cell}>
                <span className="flex items-center justify-end gap-2">
                  <span aria-hidden="true" className="block h-1 w-full max-w-24 rounded-full bg-paper-2">
                    <span className="block h-full rounded-full bg-wait" style={{ width: `${numbers.waitingShare ?? 0}%` }} />
                  </span>
                  <span className="w-10 text-right">{numbers.waitingShare == null ? "–" : `${numbers.waitingShare}%`}</span>
                </span>
              </td>
              <td className={`${cell} text-right`}>{formatRounds(numbers.rounds)}</td>
              <td className={`${cell} text-right text-live`}>{formatReverted(numbers.reverted)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="font-sans text-[12px] leading-[1.5] text-ink-3">
        Rounds is the median review rounds per merge. Reverted counts which of these merges were reverted within 14 days.
      </p>
    </section>
  );
}

function Phases({ phases }: { phases: FlowNumbers["phases"] }) {
  return (
    <details>
      <summary className={`h-[30px] cursor-pointer border-b border-hair leading-[30px] ${HEADING}`}>Every phase, in hours</summary>
      <dl className="mt-2 flex max-w-md flex-col">
        {phases.map(({ phase, hours }) => (
          <div key={phase} className="flex h-7 items-center justify-between gap-6 font-sans text-[13px]">
            <dt className="text-ink-3">{phase}</dt>
            <dd className="font-mono text-[12.5px] text-ink">{formatHours(hours)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function CheckMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 16 16" width={14} height={14} fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round"
      className="size-3.5 shrink-0 text-accent">
      <path d="m3.5 8.5 3 3 6-7" />
    </svg>
  );
}

function RangeControl({ search, choose }: { search: FlowSearch; choose: (change: FlowSearch) => void }) {
  const [draft, setDraft] = useState({ from: "", to: "" });
  const custom = search.from != null && search.to != null;
  const preset = presetOf(search.range);
  const field = "h-[30px] w-36 min-w-0 rounded-button border-0 bg-selected px-2 font-mono text-[12.5px] text-ink [color-scheme:inherit] focus:outline-2 focus:outline-offset-1 focus:outline-focus";
  const pickDay = (change: Partial<typeof draft>) => {
    const next = { ...draft, ...change };
    setDraft(next);
    if (endedRange(next.from || undefined, next.to || undefined)) choose({ range: undefined, from: next.from, to: next.to });
  };
  return (
    <ToolPopover
      label="Range choices"
      dismissOnFocusLoss
      inPane
      sideOffset={4}
      onOpenChange={(open) => { if (open) setDraft({ from: search.from ?? "", to: search.to ?? "" }); }}
      triggerLabel="Range"
      triggerClassName="inline-flex min-h-[30px] cursor-pointer items-center gap-2 rounded-button bg-selected py-1.5 pr-2 pl-2.5 text-left font-sans text-[13px] leading-[1.25] font-medium text-ink hover:bg-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      trigger={
        <>
          {custom ? dayRangeLabel(search.from, search.to) : preset.label}
          <span className="flex text-ink-3"><DropIcon /></span>
        </>
      }
      width={220}
      className={`flex flex-col gap-1 p-1 ${MENU}`}
    >
      {(close) => (
        <>
          <Listbox
            aria-label="Range"
            activation="manual"
            deselectable
            value={custom ? null : preset.value}
            onValueChange={(next, details) => {
              const value = next ?? (custom ? undefined : preset.value);
              if (next == null) details.cancel();
              if (value != null) choose({ range: value === DEFAULT_RANGE ? undefined : value, from: undefined, to: undefined });
              close();
            }}
            className="flex flex-col outline-none"
          >
            {RANGES.map((each) => (
              <ListboxItem
                key={each.value}
                value={each.value}
                className="flex h-[26px] cursor-pointer items-center gap-2 rounded-button px-2 text-left font-sans text-[13px] text-ink-2 outline-none hover:bg-hover hover:text-ink data-selected:bg-hover data-selected:font-medium data-selected:text-ink focus-visible:bg-hover focus-visible:text-ink focus-visible:outline-2 focus-visible:outline-focus"
              >
                {each.label}
                <ListboxItemIndicator className="ml-auto inline-flex items-center justify-center">
                  <CheckMark />
                </ListboxItemIndicator>
              </ListboxItem>
            ))}
          </Listbox>
          <div className="flex flex-col gap-1.5 border-t border-hair px-2 pt-2 pb-1.5 font-sans text-[12px] text-ink-3">
            <label className="flex items-center justify-between gap-3">
              From
              <input type="date" value={draft.from} max={draft.to || undefined} className={field} onChange={(event) => pickDay({ from: event.target.value })} />
            </label>
            <label className="flex items-center justify-between gap-3">
              To
              <input type="date" value={draft.to} min={draft.from || undefined} max={lastEndedDay()} className={field} onChange={(event) => pickDay({ to: event.target.value })} />
            </label>
            <span className="leading-[1.5]">Whole days back from To, one to 182; To is a past day.</span>
          </div>
        </>
      )}
    </ToolPopover>
  );
}

function byProject(answers: { name: string; flow: Flow }[]): ProjectFlow[] {
  const grouped = new Map<string, { name: string; flow: Flow }[]>();
  for (const answer of answers) grouped.set(answer.name, [...(grouped.get(answer.name) ?? []), answer]);
  return [...grouped].map(([name, group]) => ({ name, numbers: flowNumbers(sameWindow(group).map(({ flow }) => flow)) }));
}

export function FlowPage() {
  const { hosts } = useConsolePeers();
  const search = useSearch({ from: "/flow" });
  const { project } = search;
  const navigate = useNavigate();
  const deps = useContext(PollDepsContext);
  const asked = flowWindow(search);
  const answers = useFlows(hosts, deps, asked).map(({ host, flow }) => ({ host, name: projectName(host.status?.project ?? host.project ?? host.label), flow }));
  const inScope = new Set(scopedHosts(hosts, project));
  const scoped = sameWindow(answers.filter(({ host }) => inScope.has(host))).map(({ flow }) => flow);
  const numbers = scoped.length > 0 ? flowNumbers(scoped) : null;
  const weekly = flowWeekly(scoped);
  const span = flowSpan(scoped);
  const names = [...new Set(hosts.flatMap((host) => (host.project == null ? [] : [projectName(host.project)])))];
  const pick = (name: string | undefined) => {
    storeScope(name ?? null);
    void navigate({ to: "/flow", search: (previous: ScopeSearch) => ({ ...previous, project: name }) });
  };
  const choose = (change: FlowSearch) => void navigate({ to: "/flow", search: (previous: ScopeSearch & FlowSearch) => ({ ...previous, ...change }) });
  return (
    <section aria-labelledby="page-title" className="flex max-w-[1180px] flex-col gap-8 px-8 pt-3.5 pb-10 max-[720px]:px-4">
      <PageHeader
        title="Flow"
        count={span == null ? undefined : { text: span }}
        tools={
          <>
            <Picker label="Project" options={[{ value: ALL, label: "All projects" }, ...names.map((name) => ({ value: name, label: name }))]}
              value={project ?? ALL} onPick={(value) => pick(value === ALL ? undefined : value)} />
            <RangeControl search={search} choose={choose} />
          </>
        }
      />
      {numbers && (
        <>
          <Numbers numbers={numbers} weekly={weekly} bucket={asked.bucket} />
          <TimeSplit numbers={numbers} />
          {weekly.starts.length > 0 && <WeekByWeek weekly={weekly} allProjects={project == null} bucket={asked.bucket} />}
          <div className="grid grid-cols-2 gap-10 max-[960px]:grid-cols-1">
            <BiggestWaits waits={numbers.waits} />
            <ProjectsTable projects={byProject(answers)} scope={project} pick={pick} />
          </div>
          <Phases phases={numbers.phases} />
        </>
      )}
    </section>
  );
}
