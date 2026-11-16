import { zoneName } from "./runShared";
import { useContext, useEffect, useRef, useState, type RefObject } from "react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { Btn } from "../components/Btn";
import { ChevronIcon } from "../components/icons";
import { dayRangeLabel, FilterChips, FilterPopover, PressedGroup, RangePopover, SearchToggle, type FilterChip, type FilterSection, type RangePreset, type SortSection } from "../components/ListFilters";
import { RunTable } from "../components/RunTable";
import {
  DEFAULT_RANGE, DEFAULT_SIZE, FAILURE_KINDS, listOf, PAGE_SIZES, RUN_GROUPS, useRunPage,
  type RunGroup, type RunOrder, type RunRange, type RunSort, type RunsFilters, type RunsSearch,
} from "../history";
import { projectColours } from "../dayLog";
import { PollDepsContext, useConsolePeers, type ScopeSearch } from "../scope";

const STATE_LABELS: Record<RunGroup, string> = { active: "Active", merged: "Merged", failed: "Failed", abandoned: "Abandoned" };

const FAILURE_LABELS: Record<(typeof FAILURE_KINDS)[number], string> = {
  verify: "verify",
  review_route: "review",
  fix_no_progress: "no progress on a fix",
  no_commits: "no commits",
  budget: "past its time box",
  merge_lock: "merge",
  swept: "swept",
  infra: "infra",
  unclassified: "unclassified",
};

const RANGES: (RangePreset & { value: RunRange; chip: string })[] = [
  { value: "today", label: "Today", name: "Today", back: 0, chip: "Today" },
  { value: "2d", label: "2d", name: "2 days", back: 1, chip: "Last 2 days" },
  { value: "7d", label: "7d", name: "7 days", back: 6, chip: "Last 7 days" },
  { value: "30d", label: "30d", name: "30 days", back: 29, chip: "Last 30 days" },
  { value: "any", label: "Any", name: "Any time", back: null, chip: "Any time" },
];

const CHIP_WORDS = { projects: "Project", failure: "Failure", implementer: "Implementer" } as const;

const CLEARED = {
  q: undefined, projects: undefined, state: undefined, failure: undefined, implementer: undefined, range: undefined, from: undefined, to: undefined,
} satisfies RunsSearch & RunsFilters;

const SORT_WORDS: Record<RunSort, Record<RunOrder, string>> = {
  started: { desc: "newest first", asc: "oldest first" },
  ended: { desc: "last to end first", asc: "first to end first" },
  duration: { desc: "longest first", asc: "shortest first" },
  rounds: { desc: "most rounds first", asc: "fewest rounds first" },
  state: { asc: "active first", desc: "abandoned first" },
};

const SORT_LABELS: Record<RunSort, string> = { started: "Started", ended: "Ended", duration: "Took", rounds: "Rounds", state: "State" };

const HIDE_AFTER_PX = 24;
const QUIET_MS = 150;

/** How far the dock rises to hide the controls row, null while it shows:
 *  it hides after a run of scrolling down and shows after one up, once the
 *  rows have passed under the stuck dock by the row's height. */
function useControlsShift(dock: RefObject<HTMLDivElement | null>, controls: RefObject<HTMLDivElement | null>): number | null {
  const { now } = useContext(PollDepsContext);
  const [shift, setShift] = useState<number | null>(null);
  const track = useRef({ top: 0, run: 0, quietUntil: Number.NEGATIVE_INFINITY, hidden: false });
  useEffect(() => {
    const area = dock.current?.closest("main");
    if (!area) return;
    const t = track.current;
    t.top = area.scrollTop;
    const toggle = (next: boolean) => {
      Object.assign(t, { hidden: next, run: 0, quietUntil: now() + QUIET_MS });
      setShift(next ? (controls.current?.offsetHeight ?? 0) : null);
    };
    const onScroll = () => {
      const top = area.scrollTop;
      const step = top - t.top;
      t.top = top;
      const below = dock.current?.nextElementSibling;
      const rowsTop = (below?.getBoundingClientRect().top ?? 0) - area.getBoundingClientRect().top;
      const docked = top > 0 && rowsTop <= (dock.current?.offsetHeight ?? 0) - (controls.current?.offsetHeight ?? 0);
      if (!docked) {
        t.run = 0;
        if (t.hidden) toggle(false);
        return;
      }
      if (step === 0 || now() < t.quietUntil) return;
      t.run = Math.sign(step) === Math.sign(t.run) ? t.run + step : step;
      if (!t.hidden && t.run >= HIDE_AFTER_PX) toggle(true);
      else if (t.hidden && t.run <= -HIDE_AFTER_PX) toggle(false);
    };
    area.addEventListener("scroll", onScroll, { passive: true });
    return () => area.removeEventListener("scroll", onScroll);
  }, [dock, controls, now]);
  return shift;
}

function StateTabs({ state, counts, onPick }: { state: string | undefined; counts: Record<RunGroup, number>; onPick: (group: RunGroup | undefined) => void }) {
  const all = RUN_GROUPS.reduce((sum, group) => sum + counts[group], 0);
  const tabs = [{ group: undefined, label: "All", count: all }, ...RUN_GROUPS.map((group) => ({ group, label: STATE_LABELS[group], count: counts[group] }))];
  return (
    <div role="group" aria-label="Run states" className="flex flex-wrap items-center gap-1">
      {tabs.map((tab) => (
        <button
          key={tab.label}
          type="button"
          aria-pressed={state === tab.group}
          onClick={() => onPick(tab.group)}
          className="inline-flex min-h-8 cursor-pointer items-center gap-2 rounded-chip px-2.5 font-sans text-[13.5px] font-medium text-ink-2 hover:bg-hover hover:text-ink aria-pressed:bg-hover aria-pressed:text-ink"
        >
          {tab.label} <span className="font-mono text-[12px] font-normal tabular-nums text-ink-3">{tab.count}</span>
        </button>
      ))}
    </div>
  );
}

function chipsOf(search: RunsSearch & RunsFilters, sections: FilterSection[]): FilterChip<Partial<typeof CLEARED>>[] {
  const chips: FilterChip<Partial<typeof CLEARED>>[] = [];
  if (search.q) chips.push({ word: "Search", value: `“${search.q}”`, clears: { q: undefined } });
  for (const section of sections) {
    const param = section.param as keyof typeof CHIP_WORDS;
    const checked = listOf(search[param]);
    const labels = section.options.filter((option) => checked.includes(option.value)).map((option) => option.label);
    if (labels.length > 0) chips.push({ word: CHIP_WORDS[param], value: labels.join(", "), clears: { [param]: undefined } });
  }
  const states = listOf(search.state);
  if (states.length > 1) chips.push({ word: "State", value: RUN_GROUPS.filter((group) => states.includes(group)).map((group) => STATE_LABELS[group]).join(", "), clears: { state: undefined } });
  const started = search.from != null || search.to != null ? dayRangeLabel(search.from, search.to) : RANGES.find((each) => each.value === search.range)?.chip;
  if (started) chips.push({ word: "Started", value: started, clears: { range: undefined, from: undefined, to: undefined } });
  return chips;
}

export function Runs() {
  const search = useSearch({ from: "/runs" });
  const { project } = search;
  const sort: RunSort = search.sort ?? "started";
  const order: RunOrder = search.order ?? "desc";
  const page = search.page ?? 1;
  const size = search.size ?? DEFAULT_SIZE;
  const { now, rows, total, counts, implementers, projects, settled } = useRunPage({ ...search, state: search.state ?? "", sort, order, page, size });
  const colours = projectColours(useConsolePeers().hosts);
  const navigate = useNavigate();
  const dock = useRef<HTMLDivElement>(null);
  const controls = useRef<HTMLDivElement>(null);
  const shift = useControlsShift(dock, controls);
  const hidden = shift != null;
  const go = (next: RunsSearch & RunsFilters) =>
    void navigate({ to: ".", search: (previous: ScopeSearch & RunsSearch & RunsFilters) => ({ ...previous, page: undefined, ...next }) });
  const [fieldKey, setFieldKey] = useState(0);
  const clearAll = () => {
    setFieldKey((key) => key + 1);
    go(CLEARED);
  };
  const sortBy = (key: RunSort, nextOrder: RunOrder) => {
    const fresh = key === "started" && nextOrder === "desc";
    go({ sort: fresh ? undefined : key, order: fresh ? undefined : nextOrder });
  };
  const sorting: SortSection = {
    keys: (Object.keys(SORT_LABELS) as RunSort[]).map((key) => ({ value: key, label: SORT_LABELS[key] })),
    orders: (["desc", "asc"] as const).map((each) => ({ value: each, label: SORT_WORDS[sort][each] })),
    sort,
    order,
    onSort: (key) => sortBy(key as RunSort, key === "state" ? "asc" : "desc"),
    onOrder: (picked) => sortBy(sort, picked as RunOrder),
  };
  const sections: FilterSection[] = [
    { label: "Project", param: "projects", options: projects.map(({ name, count }) => ({ value: name, label: name, count, colour: colours.get(name) })) },
    { label: "Failure kind", param: "failure", options: FAILURE_KINDS.map((kind) => ({ value: kind, label: FAILURE_LABELS[kind], count: counts.failure[kind] ?? 0 })) },
    { label: "Implementer", param: "implementer", options: implementers.map((name) => ({ value: name, label: name, count: counts.implementer[name] ?? 0 })) },
  ];
  const chips = chipsOf(search, sections);
  const first = (page - 1) * size;
  const backTo = Math.min(page - 1, Math.ceil(total / size));
  const pastEnd = settled && rows.length === 0 && page > 1;
  useEffect(() => {
    if (!pastEnd) return;
    void navigate({ to: ".", replace: true, search: (previous: ScopeSearch & RunsSearch) => ({ ...previous, page: backTo > 1 ? backTo : undefined }) });
  }, [pastEnd, backTo, navigate]);
  return (
    <section aria-labelledby="page-title" className="@container flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-2">
      <header className="flex flex-col gap-[18px]">
        <div className="flex min-h-[30px] items-center gap-x-5 gap-y-3">
          <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] tracking-[-0.01em] text-ink">Runs</h1>
          <div data-tools className="relative ml-auto flex min-w-0 items-center gap-1">
            <SearchToggle key={fieldKey} label="Search runs" />
            <RangePopover presets={RANGES} fallback={DEFAULT_RANGE} hidden={hidden} now={now} />
            <FilterPopover icon label="Filter and sort" sections={sections} hidden={hidden} sorting={sorting}
              extra={<PressedGroup label="Per page" options={PAGE_SIZES} value={size} onPick={(picked) => go({ size: picked === DEFAULT_SIZE ? undefined : picked })} />} />
          </div>
        </div>
        <StateTabs state={search.state} counts={counts.state} onPick={(group) => go({ state: group })} />
      </header>
      <RunTable
        rows={rows}
        now={now}
        label={`Runs, ${SORT_WORDS[sort][order]}`}
        grouped={sort === "started"}
        empty={chips.length > 0 || search.state != null ? "No runs match" : `No runs in ${project ?? "any project"} in the last 7 days.`}
        dock={
          <div
            ref={dock}
            data-runs-dock
            style={{ transform: `translateY(${-(shift ?? 0)}px)` }}
            className="top-0 z-10 bg-canvas transition-transform duration-200 ease-(--ease-console) @min-[1100px]:sticky"
          >
            <div
              ref={controls}
              data-controls
              data-hidden={hidden ? "" : undefined}
              inert={hidden}
              className="flex flex-col gap-2.5 pb-3.5 transition-[opacity,visibility] duration-200 empty:pb-0 data-hidden:invisible data-hidden:opacity-0"
            >
              {chips.length > 0 && <FilterChips chips={chips} onClear={go} onClearAll={clearAll} />}
            </div>
          </div>
        }
      />
      {rows.length > 0 && (
        <nav aria-label="Pages of runs" className="-mt-2 flex flex-wrap items-center justify-end gap-x-3 gap-y-2">
          <span className="font-mono text-[12.5px] text-ink-2">
            {first + 1}–{first + rows.length} of {total}
          </span>
          <Btn disabled={page === 1} onClick={() => go({ page: page === 2 ? undefined : page - 1 })}>
            <ChevronIcon folded={false} /> Previous
          </Btn>
          <Btn disabled={first + rows.length >= total} onClick={() => go({ page: page + 1 })}>
            Next <ChevronIcon folded />
          </Btn>
        </nav>
      )}
      <footer className="flex flex-wrap gap-x-[18px] gap-y-1.5 font-mono text-[12px] leading-[1.4] text-ink-3">
        <span>Active runs also show on the Overview. Open a run for its stage, log and agent.</span>
        <span>times show in {zoneName(now)}</span>
      </footer>
    </section>
  );
}
