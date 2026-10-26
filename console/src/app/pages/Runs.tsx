import { zoneName } from "./runShared";
import { useContext, useEffect, useRef, useState, type RefObject } from "react";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { Btn } from "../components/Btn";
import { ChevronIcon } from "../components/icons";
import { RunTable, RunTableHeads } from "../components/RunTable";
import { DEFAULT_SIZE, PAGE_SIZES, useRunPage, type RunFilter, type RunOrder, type RunSort, type RunsSearch } from "../history";
import { storeScope } from "../projects";
import { PollDepsContext, type ScopeSearch } from "../scope";

const FILTERS: { id: RunFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "active", label: "Active" },
  { id: "merged", label: "Merged" },
  { id: "failed", label: "Failed" },
  { id: "abandoned", label: "Abandoned" },
];

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

export function Runs() {
  const search = useSearch({ from: "/runs" });
  const { project } = search;
  const filter: RunFilter = search.state ?? "active";
  const sort: RunSort = search.sort ?? "started";
  const order: RunOrder = search.order ?? "desc";
  const page = search.page ?? 1;
  const size = search.size ?? DEFAULT_SIZE;
  const { now, rows, total, counts } = useRunPage({ state: filter, sort, order, page, size });
  const navigate = useNavigate();
  const dock = useRef<HTMLDivElement>(null);
  const controls = useRef<HTMLDivElement>(null);
  const shift = useControlsShift(dock, controls);
  const hidden = shift != null;
  const word = filter === "all" ? "" : `${filter} `;
  const orderWords = SORT_WORDS[sort][order];
  const go = (next: RunsSearch) =>
    void navigate({ to: ".", search: (previous: ScopeSearch & RunsSearch) => ({ ...previous, page: undefined, ...next }) });
  const sortBy = (key: RunSort) => {
    const nextOrder: RunOrder = key === sort ? (order === "asc" ? "desc" : "asc") : key === "state" ? "asc" : "desc";
    const fresh = key === "started" && nextOrder === "desc";
    go({ sort: fresh ? undefined : key, order: fresh ? undefined : nextOrder });
  };
  const first = (page - 1) * size;
  return (
    <section aria-labelledby="page-title" className="@container flex flex-col gap-6 px-8 pt-3.5 pb-7 max-[720px]:px-2">
      <header className="flex flex-col gap-1.5 border-b border-rule pb-[18px]">
        <h1 id="page-title" className="font-sans text-[20px] font-semibold leading-[1.25] text-ink">Runs</h1>
        <p data-count-line className="font-mono text-[13px] leading-[1.6] [overflow-wrap:anywhere] text-ink-2">
          {project ?? "all projects"} · {total} runs
          <Slash />
          <b className="font-semibold text-ink">{counts.active}</b> active
          <Slash />
          {orderWords}
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
      <RunTable
        rows={rows}
        now={now}
        label={`Runs, ${orderWords}`}
        empty={`No ${word}runs in ${project ?? "any project"} today or yesterday.`}
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
              className="flex flex-wrap items-center gap-x-4 gap-y-2.5 pt-1 pb-3.5 transition-[opacity,visibility] duration-200 data-hidden:invisible data-hidden:opacity-0 @max-[1099px]:sticky @max-[1099px]:left-0 @max-[1099px]:w-[100cqw]"
            >
              <span id="runs-state-label" className="font-mono text-[11px] font-medium uppercase leading-none tracking-[0.12em] text-ink-3">
                State
              </span>
              <div role="group" aria-labelledby="runs-state-label" className="inline-flex flex-wrap border border-ink-3">
                {FILTERS.map(({ id, label }) => (
                  <button
                    key={id}
                    type="button"
                    aria-pressed={filter === id}
                    onClick={() => go({ state: id })}
                    className="min-h-9 cursor-pointer px-3 font-mono text-[11px] font-medium uppercase leading-none tracking-[0.1em] text-ink-2 aria-pressed:bg-ink aria-pressed:text-paper"
                  >
                    {label} <b className="font-medium tracking-normal">{counts[id]}</b>
                  </button>
                ))}
              </div>
              <span className="ml-auto font-mono text-[12.5px] text-ink-3">{total} runs</span>
              <select
                aria-label="Rows a page"
                value={size}
                onChange={(event) => {
                  const picked = Number(event.target.value);
                  go({ size: picked === DEFAULT_SIZE ? undefined : picked });
                }}
                className="min-h-9 cursor-pointer border border-rule bg-paper px-2.5 font-sans text-[13px] text-ink focus:outline-2 focus:-outline-offset-1 focus:outline-focus"
              >
                {PAGE_SIZES.map((each) => (
                  <option key={each} value={each}>
                    {each} a page
                  </option>
                ))}
              </select>
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
