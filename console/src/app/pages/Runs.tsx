import { zoneName } from "./runShared";
import { useContext, useEffect, useRef, useState, type RefObject } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { Btn } from "../components/Btn";
import { ChevronIcon } from "../components/icons";
import { dayRangeLabel, FilterPopover, RangeButtons, SearchField, type FilterSection } from "../components/ListFilters";
import { RunTable, RunTableHeads } from "../components/RunTable";
import {
  DEFAULT_RANGE, DEFAULT_SIZE, FAILURE_KINDS, listOf, PAGE_SIZES, RUN_GROUPS, useRunPage,
  type RunGroup, type RunOrder, type RunRange, type RunSort, type RunsFilters, type RunsSearch,
} from "../history";
import { PollDepsContext, type ScopeSearch } from "../scope";

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

const RANGES: { value: RunRange; label: string; chip: string }[] = [
  { value: "today", label: "Today", chip: "Today" },
  { value: "2d", label: "2d", chip: "Last 2 days" },
  { value: "7d", label: "7d", chip: "Last 7 days" },
  { value: "30d", label: "30d", chip: "Last 30 days" },
  { value: "any", label: "Any", chip: "Any time" },
];

const CHIP_WORDS = { projects: "Project", state: "State", failure: "Failure", implementer: "Implementer" } as const;

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

const HIDE_AFTER_PX = 24;
const QUIET_MS = 150;

function Slash() {
  return <span className="text-ink-3"> / </span>;
}

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

interface Chip {
  text: string;
  clears: Partial<typeof CLEARED>;
}

function chipsOf(search: RunsSearch & RunsFilters, sections: FilterSection[]): Chip[] {
  const chips: Chip[] = [];
  if (search.q) chips.push({ text: `Search: “${search.q}”`, clears: { q: undefined } });
  for (const section of sections) {
    const param = section.param as keyof typeof CHIP_WORDS;
    const checked = listOf(search[param]);
    const labels = section.options.filter((option) => checked.includes(option.value)).map((option) => option.label);
    if (labels.length > 0) chips.push({ text: `${CHIP_WORDS[param]}: ${labels.join(", ")}`, clears: { [param]: undefined } });
  }
  const started = search.from != null || search.to != null ? dayRangeLabel(search.from, search.to) : RANGES.find((each) => each.value === search.range)?.chip;
  if (started) chips.push({ text: `Started: ${started}`, clears: { range: undefined, from: undefined, to: undefined } });
  return chips;
}

function Chips({ chips, onClear }: { chips: Chip[]; onClear: (clears: Partial<typeof CLEARED>) => void }) {
  return (
    <div data-chips className="flex flex-wrap items-center gap-2">
      {chips.map((chip) => {
        const [word, ...rest] = chip.text.split(": ");
        return (
          <span key={word} className="inline-flex min-h-7 items-center gap-2 border border-rule bg-paper pr-1 pl-2.5 font-mono text-[12px] text-ink-2">
            <span>
              {word}: <b className="font-semibold text-ink">{rest.join(": ")}</b>
            </span>
            <button type="button" aria-label={`Remove ${chip.text}`} onClick={() => onClear(chip.clears)}
              className="inline-flex size-6 cursor-pointer items-center justify-center text-ink-3 hover:text-ink">
              <svg aria-hidden="true" viewBox="0 0 10 10" width={9} height={9} stroke="currentColor" strokeWidth={1.4}>
                <path d="M1 1l8 8M9 1l-8 8" />
              </svg>
            </button>
          </span>
        );
      })}
      <button type="button" onClick={() => onClear(CLEARED)}
        className="ml-1 min-h-7 cursor-pointer font-sans text-[13px] font-medium text-ink underline underline-offset-2 hover:text-act">
        Clear filters
      </button>
    </div>
  );
}

export function Runs() {
  const search = useSearch({ from: "/runs" });
  const { project } = search;
  const sort: RunSort = search.sort ?? "started";
  const order: RunOrder = search.order ?? "desc";
  const page = search.page ?? 1;
  const size = search.size ?? DEFAULT_SIZE;
  const { now, rows, total, counts, implementers, projects, picked, settled } = useRunPage({ ...search, state: search.state ?? "", sort, order, page, size });
  const navigate = useNavigate();
  const dock = useRef<HTMLDivElement>(null);
  const controls = useRef<HTMLDivElement>(null);
  const shift = useControlsShift(dock, controls);
  const hidden = shift != null;
  const orderWords = SORT_WORDS[sort][order];
  const go = (next: RunsSearch & RunsFilters) =>
    void navigate({ to: ".", search: (previous: ScopeSearch & RunsSearch & RunsFilters) => ({ ...previous, page: undefined, ...next }) });
  const sortBy = (key: RunSort) => {
    const nextOrder: RunOrder = key === sort ? (order === "asc" ? "desc" : "asc") : key === "state" ? "asc" : "desc";
    const fresh = key === "started" && nextOrder === "desc";
    go({ sort: fresh ? undefined : key, order: fresh ? undefined : nextOrder });
  };
  const sections: FilterSection[] = [
    { label: "Project", param: "projects", options: projects.map(({ name, count }) => ({ value: name, label: name, count })) },
    { label: "State", param: "state", options: RUN_GROUPS.map((group) => ({ value: group, label: STATE_LABELS[group], count: counts.state[group] })) },
    { label: "Failure kind", param: "failure", options: FAILURE_KINDS.map((kind) => ({ value: kind, label: FAILURE_LABELS[kind], count: counts.failure[kind] ?? 0 })) },
    { label: "Implementer", param: "implementer", options: implementers.map((name) => ({ value: name, label: name, count: counts.implementer[name] ?? 0 })) },
  ];
  const chips = chipsOf(search, sections);
  const scope = picked.length === 1 ? picked[0] : picked.length > 1 ? `${picked.length} projects` : (project ?? "all projects");
  const first = (page - 1) * size;
  const backTo = Math.min(page - 1, Math.ceil(total / size));
  const pastEnd = settled && rows.length === 0 && page > 1;
  useEffect(() => {
    if (!pastEnd) return;
    void navigate({ to: ".", replace: true, search: (previous: ScopeSearch & RunsSearch) => ({ ...previous, page: backTo > 1 ? backTo : undefined }) });
  }, [pastEnd, backTo, navigate]);
  return (
    <section aria-labelledby="page-title" className="@container flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-2">
      <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Runs</h1>
        <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">
          {scope} · <b className="font-semibold text-ink">{total}</b> runs
          <Slash />
          <b className="font-semibold text-ink">{counts.state.active}</b> active
          <Slash />
          {orderWords}
        </p>
      </header>
      <RunTable
        rows={rows}
        now={now}
        label={`Runs, ${orderWords}`}
        empty={chips.length > 0 ? "No runs match" : `No runs in ${project ?? "any project"} in the last 7 days.`}
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
              className="flex flex-col gap-2.5 pt-1 pb-3.5 transition-[opacity,visibility] duration-200 data-hidden:invisible data-hidden:opacity-0 @max-[1099px]:sticky @max-[1099px]:left-0 @max-[1099px]:w-[100cqw]"
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-2.5 @min-[1100px]:flex-nowrap">
              <SearchField label="Search runs" width="w-[220px] min-w-[112px] shrink" />
              <RangeButtons label="Started" presets={RANGES} fallback={DEFAULT_RANGE} hidden={hidden} />
              <FilterPopover label="Filter runs" sections={sections} hidden={hidden} />
              <span className="ml-auto shrink-0 font-mono text-[12.5px] whitespace-nowrap text-ink-3">{total} runs</span>
              <select
                aria-label="Rows a page"
                value={size}
                onChange={(event) => {
                  const picked = Number(event.target.value);
                  go({ size: picked === DEFAULT_SIZE ? undefined : picked });
                }}
                className="min-h-9 shrink-0 cursor-pointer border border-rule bg-paper px-2.5 font-sans text-[13px] text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
              >
                {PAGE_SIZES.map((each) => (
                  <option key={each} value={each}>
                    {each} a page
                  </option>
                ))}
              </select>
              </div>
              {chips.length > 0 && <Chips chips={chips} onClear={go} />}
            </div>
            <RunTableHeads sort={sort} order={order} onSort={sortBy} />
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
        <Link to="/runs/history" search={{ project }} className="text-act underline underline-offset-2">Older days</Link>
        <span>times show in {zoneName(now)}</span>
      </footer>
    </section>
  );
}
