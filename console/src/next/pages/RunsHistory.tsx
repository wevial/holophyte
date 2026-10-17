import { useContext } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { formatSettled } from "../../lib/format";
import { zoneName } from "./runShared";
import { stateGroup } from "../history";
import { DayRunTable } from "../components/DayRunTable";
import { Btn } from "../components/Btn";
import { ChevronIcon } from "../components/icons";
import { useHistoryDays, useHistoryTotal } from "../queries";
import { dayGroups, PAGE_COUNT, PAGE_DAYS, pastDays } from "../runDays";
import { PollDepsContext, scopedHosts, useConsolePeers } from "../scope";

type Outcome = "merged" | "failed" | "abandoned";

const FILTERS = [
  { id: "all", label: "All" },
  { id: "merged", label: "Merged" },
  { id: "failed", label: "Failed" },
  { id: "abandoned", label: "Abandoned" },
] as const;

export function validateHistorySearch({ page, outcome }: Record<string, unknown>): { page?: number; outcome?: Outcome } {
  const number = Number(page);
  return {
    page: Number.isInteger(number) && number > 1 && number <= PAGE_COUNT ? number : undefined,
    outcome: outcome === "merged" || outcome === "failed" || outcome === "abandoned" ? outcome : undefined,
  };
}

const dayName = new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "numeric", month: "short" });
const rangeName = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" });

function DayHead({ day }: { day: ReturnType<typeof dayGroups>[number] }) {
  return (
    <header className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-rule px-4 pt-3.5 pb-3">
      <h2 id={`day-${day.since}`} className="font-sans text-[16px] font-semibold text-ink">{dayName.format(day.since)}</h2>
      <div className="flex flex-wrap gap-x-3 gap-y-1 font-mono text-[12.5px] text-ink-3">
        <span><b className="font-semibold text-ink">{day.counts.runs}</b> runs</span>
        <span><b className="font-semibold text-live">{day.counts.merged}</b> merged</span>
        <span><b className={`font-semibold ${day.counts.failed > 0 ? "text-bad" : "text-ink-2"}`}>{day.counts.failed}</b> failed</span>
        {day.counts.abandoned > 0 && <span><b className="font-semibold">{day.counts.abandoned}</b> abandoned</span>}
      </div>
      {day.workMs != null && day.estimateMs != null && (
        <p className="ml-auto font-mono text-[12px] text-ink-3">
          {formatSettled(day.workMs)} of work against {formatSettled(day.estimateMs)} estimated
        </p>
      )}
    </header>
  );
}

export function RunsHistory() {
  const { project, page: pickedPage, outcome } = useSearch({ from: "/runs/history" });
  const page = pickedPage ?? 1;
  const navigate = useNavigate();
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const hosts = scopedHosts(peers.hosts, project);
  const days = pastDays(peers.now).slice((page - 1) * PAGE_DAYS, page * PAGE_DAYS);
  const { answers } = useHistoryDays(hosts, days, deps);
  const { total } = useHistoryTotal(hosts, peers.now, deps);
  const groups = dayGroups(hosts, answers, days);
  const counts = { all: 0, merged: 0, failed: 0, abandoned: 0 };
  for (const day of groups) {
    counts.all += day.counts.runs;
    counts.merged += day.counts.merged;
    counts.failed += day.counts.failed;
    counts.abandoned += day.counts.abandoned;
  }
  const shown = groups.map(day => ({ ...day, rows: outcome ? day.rows.filter(row => stateGroup(row.state) === outcome) : day.rows }));
  const hasRows = shown.some(day => day.rows.length > 0);
  const runWords = `No ${outcome ? `${outcome} ` : ""}runs${project ? ` in ${project}` : ""}`;
  const range = `${rangeName.format(days[0]!.since)} to ${rangeName.format(days[PAGE_DAYS - 1]!.since)}`;
  const pickOutcome = (id: Outcome | "all") => void navigate({
    to: "/runs/history", search: { project, page: pickedPage, outcome: id === "all" ? undefined : id },
  });
  const pick = (next: number) => void navigate({ to: "/runs/history", search: { project, outcome, page: next === 1 ? undefined : next } });
  return (
    <section aria-labelledby="page-title" className="mx-auto flex max-w-[1320px] flex-col gap-[18px] px-8 py-7 max-[720px]:px-2">
      <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Runs history</h1>
        <Link to="/runs" search={{ project }} className="self-start font-mono text-[12.5px] text-act underline underline-offset-2">Today and yesterday</Link>
        <p className="font-mono text-[13px] leading-[1.6] text-ink-2 [overflow-wrap:anywhere]">
          {project ?? "all projects"} · {total != null ? `${total} runs in the` : "the"} 30 days before yesterday · newest day first
        </p>
      </header>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
          <span id="history-outcome-label" className="font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3">Outcome</span>
          <div role="group" aria-labelledby="history-outcome-label" className="inline-flex flex-wrap border border-ink-3">
            {FILTERS.map(({ id, label }) => (
              <button key={id} type="button" aria-pressed={(outcome ?? "all") === id} onClick={() => pickOutcome(id)}
                className="min-h-9 cursor-pointer px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.1em] text-ink-2 aria-pressed:bg-ink aria-pressed:text-paper">
                {label} <b className="font-medium tracking-normal">{counts[id]}</b>
              </button>
            ))}
          </div>
        </div>
      <nav aria-label="Pages of days" className="ml-auto flex max-w-full flex-wrap items-center justify-end gap-x-3 gap-y-2">
        <Btn disabled={page === 1} onClick={() => pick(page - 1)}><ChevronIcon folded={false} /> Newer days</Btn>
        <span className="font-mono text-[12.5px] text-ink-2">{range} · page {page} of {PAGE_COUNT}</span>
        <Btn disabled={page === PAGE_COUNT} onClick={() => pick(page + 1)}>Older days <ChevronIcon folded /></Btn>
      </nav>
      </div>
      {!hasRows && <p className="border border-rule bg-paper px-4 py-4 font-mono text-[13px] text-ink-3 [overflow-wrap:anywhere]">{runWords} from {range}.{page < PAGE_COUNT ? " Try older days." : ""}</p>}
      {hasRows && shown.map(day => (
        <section key={day.since} aria-labelledby={`day-${day.since}`} className="min-w-0 border border-rule bg-paper">
          <DayHead day={day} />
          {day.rows.length > 0 && <DayRunTable day={day} label={dayName.format(day.since)} />}
          {day.rows.length === 0 && <p className="px-4 py-4 font-mono text-[12.5px] text-ink-3 [overflow-wrap:anywhere]">{runWords} this day.</p>}
        </section>
      ))}
      <footer className="flex flex-wrap gap-x-[18px] gap-y-1.5 border-b border-rule pb-[18px] font-mono text-[12px] leading-[1.4] text-ink-3">
        <span>the bar runs to 1.5× the estimate; the tick marks the estimate</span>
        <span>five days a page · 30 days back</span>
        <span>times {zoneName(peers.now)}</span>
      </footer>
    </section>
  );
}
