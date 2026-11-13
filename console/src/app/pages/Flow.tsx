import { useContext, type ReactNode } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import type { Flow } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { BarChart, Change, LineChart, Sparkline, type TrendNumber } from "../components/FlowCharts";
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
  type FlowNumbers,
  type FlowWeekly,
} from "../flow";
import { storeScope } from "../projects";
import { useFlows } from "../queries";
import { PollDepsContext, scopedHosts, useConsolePeers, type ScopeSearch } from "../scope";

const H2 = "font-sans text-[15px] font-semibold leading-[1.3] text-ink";
const LABEL = "font-mono text-[11px] font-medium uppercase leading-[1.3] tracking-[0.1em] text-ink-3";
const INSIDE_MIN = 8;

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

function Figure({ label, children, tone = "text-ink", trend }: { label: string; children: ReactNode; tone?: string; trend?: Trend }) {
  return (
    <div className="flex flex-col gap-2 px-4 py-4 not-first:border-l not-first:border-rule">
      <dt className={LABEL}>{label}</dt>
      <dd className="flex items-center gap-3">
        <span data-number={label} className={`font-mono text-[26px] font-semibold leading-[1.15] ${tone}`}>{children}</span>
        {trend && <Sparkline values={trend.values} />}
      </dd>
      {trend && <Change number={trend.number} values={trend.values} />}
    </div>
  );
}

function Numbers({ numbers, weekly }: { numbers: FlowNumbers; weekly: FlowWeekly }) {
  return (
    <dl aria-label="Flow numbers" className="grid grid-cols-5 border border-rule bg-paper max-[960px]:grid-cols-2">
      <Figure label="Merged" trend={{ number: "merged", values: weekly.merged }}>{numbers.merged}</Figure>
      <Figure label="Waiting" tone="text-wait" trend={{ number: "waiting", values: weekly.waitingShare }}>
        {numbers.waitingShare == null ? "–" : `${numbers.waitingShare}%`}
      </Figure>
      <Figure label="Rounds per merge" trend={{ number: "rounds", values: weekly.rounds }}>{formatRounds(numbers.rounds)}</Figure>
      <Figure label="Interventions per merge" trend={{ number: "interventions", values: weekly.interventionsPerMerge }}>
        {formatPerMerge(numbers.interventionsPerMerge)}
      </Figure>
      <Figure label="Reverted" tone="text-live">{formatReverted(numbers.reverted)}</Figure>
    </dl>
  );
}

const percent = (value: number) => `${Math.round(value)}%`;
const tenths = (value: number) => value.toFixed(1);

function WeekByWeek({ weekly, allProjects }: { weekly: FlowWeekly; allProjects: boolean }) {
  const { starts } = weekly;
  return (
    <section aria-labelledby="flow-weeks" className="flex flex-col gap-3">
      <h2 id="flow-weeks" className={H2}>Week by week</h2>
      <div className="grid grid-cols-3 gap-px border border-rule bg-rule max-[960px]:grid-cols-2 max-[720px]:grid-cols-1">
        <BarChart title="Merges" note="per week" starts={starts} values={weekly.merged} format={oneDecimal} />
        <LineChart title="Time spent waiting" note="share of run time" starts={starts} values={weekly.waitingShare} top={100} format={percent} />
        {allProjects ? (
          <LineChart title="Interventions" note="per week" starts={starts} values={weekly.interventions} format={oneDecimal} />
        ) : (
          <LineChart title="Review rounds per merge" note="median" starts={starts} values={weekly.rounds} format={oneDecimal} />
        )}
        <LineChart title="Interventions per merge" note="per week" starts={starts} values={weekly.interventionsPerMerge} format={tenths} />
      </div>
    </section>
  );
}

function TimeSplit({ numbers }: { numbers: FlowNumbers }) {
  return (
    <section aria-labelledby="flow-split" className="flex flex-col gap-3">
      <header className="flex items-baseline justify-between gap-4">
        <h2 id="flow-split" className={H2}>Where run time went</h2>
        <span className="font-mono text-[12px] text-ink-3">{Math.round(numbers.total)} run-hours</span>
      </header>
      <div data-split className="flex h-12 bg-paper-2">
        {SPLIT.map(({ part, name, fill }) => {
          const width = share(numbers.hours[part], numbers.total);
          if (width <= 0) return null;
          return (
            <div key={part} data-part={part} title={name} style={{ width: `${width}%` }} className={`flex items-center overflow-hidden px-2 not-first:border-l-2 not-first:border-canvas ${fill}`}>
              {width >= INSIDE_MIN && <span className="font-mono text-[12px] font-semibold text-canvas">{Math.round(width)}%</span>}
            </div>
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
                {BY_DESIGN.has(group) && <span className="border border-rule px-1.5 font-mono text-[10.5px] text-ink-2">by design</span>}
              </span>
              <span className="font-mono text-[12px] text-ink-2">{formatHours(hours)}</span>
            </span>
            <span aria-hidden="true" className="block h-1 bg-paper-2">
              <span className={`block h-full ${index === 0 ? "bg-need" : "bg-wait"}`} style={{ width: `${share(hours, top)}%` }} />
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
  const cell = "px-2 py-3 font-mono text-[13px] text-ink";
  const head = `${LABEL} px-2 py-2 font-medium`;
  return (
    <section aria-labelledby="flow-projects" className="flex flex-col gap-3">
      <h2 id="flow-projects" className={H2}>Projects side by side</h2>
      <table className="w-full border-collapse">
        <thead>
          <tr className="border-b border-rule text-left">
            <th scope="col" className={head}>Project</th>
            <th scope="col" className={`${head} text-right`}>Merged</th>
            <th scope="col" className={head}>Waiting</th>
            <th scope="col" className={`${head} text-right`}>Rounds</th>
            <th scope="col" className={`${head} text-right`}>Reverted</th>
          </tr>
        </thead>
        <tbody>
          {projects.map(({ name, numbers }) => (
            <tr key={name} aria-current={name === scope ? "true" : undefined} className="border-b border-rule-2 aria-[current]:bg-paper-2">
              <th scope="row" className="px-2 py-3 text-left">
                <button type="button" onClick={() => pick(name)} className="cursor-pointer font-sans text-[13px] font-semibold text-ink hover:text-act">{name}</button>
              </th>
              <td className={`${cell} text-right`}>{numbers.merged}</td>
              <td className={cell}>
                <span className="flex items-center gap-2">
                  <span aria-hidden="true" className="block h-1 w-full max-w-24 bg-paper-2">
                    <span className="block h-full bg-wait" style={{ width: `${numbers.waitingShare ?? 0}%` }} />
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
        Rounds is the median review rounds per merge. Reverted counts factory merges reverted within 14 days, over the last 30 days.
      </p>
    </section>
  );
}

function Phases({ phases }: { phases: FlowNumbers["phases"] }) {
  return (
    <details className="border-t border-rule pt-4">
      <summary className={`${H2} cursor-pointer`}>Every phase, in hours</summary>
      <dl className="mt-3 grid max-w-md grid-cols-[1fr_auto] gap-x-6 gap-y-1.5 font-mono text-[12.5px]">
        {phases.map(({ phase, hours }) => (
          <div key={phase} className="contents">
            <dt className="text-ink-2">{phase}</dt>
            <dd className="text-right text-ink">{formatHours(hours)}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function byProject(answers: { name: string; flow: Flow }[]): ProjectFlow[] {
  const grouped = new Map<string, Flow[]>();
  for (const { name, flow } of answers) grouped.set(name, [...(grouped.get(name) ?? []), flow]);
  return [...grouped].map(([name, flows]) => ({ name, numbers: flowNumbers(flows) }));
}

export function FlowPage() {
  const { hosts } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const navigate = useNavigate();
  const deps = useContext(PollDepsContext);
  const answers = useFlows(hosts, deps).map(({ host, flow }) => ({ host, name: projectName(host.status?.project ?? host.project ?? host.label), flow }));
  const inScope = new Set(scopedHosts(hosts, project));
  const scoped = answers.filter(({ host }) => inScope.has(host)).map(({ flow }) => flow);
  const numbers = scoped.length > 0 ? flowNumbers(scoped) : null;
  const weekly = flowWeekly(scoped);
  const names = [...new Set(hosts.flatMap((host) => (host.project == null ? [] : [projectName(host.project)])))];
  const pick = (name: string | undefined) => {
    storeScope(name ?? null);
    void navigate({ to: "/flow", search: (previous: ScopeSearch) => ({ ...previous, project: name }) });
  };
  return (
    <section aria-labelledby="page-title" className="flex max-w-[1180px] flex-col gap-8 px-8 pt-3.5 pb-10 max-[720px]:px-4">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Flow</h1>
          <p className="font-sans text-[13px] leading-[1.5] text-ink-2">{flowSpan(scoped) ?? " "}</p>
        </div>
        <label className="flex flex-col gap-1.5">
          <span className={LABEL}>Project</span>
          <select
            value={project ?? ""}
            onChange={(event) => pick(event.target.value === "" ? undefined : event.target.value)}
            className="min-h-9 min-w-48 cursor-pointer border border-rule bg-paper px-2.5 font-sans text-[13px] font-semibold text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
          >
            <option value="">All projects</option>
            {names.map((name) => (
              <option key={name} value={name}>{name}</option>
            ))}
          </select>
        </label>
      </header>
      {numbers && (
        <>
          <Numbers numbers={numbers} weekly={weekly} />
          <TimeSplit numbers={numbers} />
          {weekly.starts.length > 0 && <WeekByWeek weekly={weekly} allProjects={project == null} />}
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
