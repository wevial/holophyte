import { useNavigate, useSearch } from "@tanstack/react-router";
import { RunTable } from "../components/RunTable";
import { stateGroup, useRunHistory, type RunFilter, type RunsSearch } from "../history";
import { storeScope } from "../projects";
import type { ScopeSearch } from "../scope";

const FILTERS: { id: RunFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "merged", label: "Merged" },
  { id: "failed", label: "Failed" },
  { id: "abandoned", label: "Abandoned" },
];

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

function zoneName(now: number): string {
  const parts = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(now);
  return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
}

export function Runs() {
  const { now, rows } = useRunHistory();
  const { project, state } = useSearch({ from: "/runs" });
  const navigate = useNavigate();
  const filter: RunFilter = state ?? "active";
  const counts: Record<RunFilter, number> = { all: rows.length, active: 0, merged: 0, failed: 0, abandoned: 0 };
  for (const row of rows) counts[stateGroup(row.state)] += 1;
  const shown = filter === "all" ? rows : rows.filter((row) => stateGroup(row.state) === filter);
  const word = filter === "all" ? "" : `${filter} `;
  const pick = (id: RunFilter) =>
    void navigate({ to: ".", search: (previous: ScopeSearch & RunsSearch) => ({ ...previous, state: id }) });
  return (
    <section aria-labelledby="page-title" className="flex flex-col gap-6 px-8 py-7 max-[720px]:px-2">
      <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Runs</h1>
        <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">
          {project ?? "all projects"} · {rows.length} runs
          <Slash />
          <b className="font-semibold text-ink">{counts.active}</b> active
          <Slash />
          newest first
          <Slash />
          today and yesterday
        </p>
        {project != null && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3.5 gap-y-2 font-mono text-[12.5px] text-ink-2">
            <span>
              Showing <b className="font-medium [overflow-wrap:anywhere] text-act">{project}</b> only
            </span>
            <button
              type="button"
              onClick={() => {
                storeScope(null);
                void navigate({ to: ".", search: (previous: ScopeSearch) => ({ ...previous, project: undefined }) });
              }}
              className="inline-flex min-h-8 cursor-pointer items-center font-medium text-act underline underline-offset-2"
            >
              Show all projects
            </button>
          </div>
        )}
      </header>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
        <span id="runs-state-label" className="font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3">
          State
        </span>
        <div role="group" aria-labelledby="runs-state-label" className="inline-flex flex-wrap border border-ink-3">
          {FILTERS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              aria-pressed={filter === id}
              onClick={() => pick(id)}
              className="min-h-9 cursor-pointer px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.1em] text-ink-2 aria-pressed:bg-ink aria-pressed:text-paper"
            >
              {label} <b className="font-medium tracking-normal">{counts[id]}</b>
            </button>
          ))}
        </div>
      </div>
      <RunTable rows={shown} now={now} empty={`No ${word}runs in ${project ?? "any project"} today or yesterday.`} />
      <footer className="flex flex-wrap gap-x-[18px] gap-y-1.5 font-mono text-[12px] leading-[1.4] text-ink-3">
        <span>Active runs also show on the Overview. Open a run for its stage, log and agent.</span>
        <span>times {zoneName(now)}</span>
      </footer>
    </section>
  );
}
