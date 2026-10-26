import { useContext, useState, type ReactNode } from "react";
import type { LedgerEntry } from "../../lib/contract.gen";
import { projectName } from "../../lib/derive";
import { categoryOf, EVERYTHING, HIGHLIGHTS, itemsOf, LOG_GROUPS, projectColours, projectOn, useDayLogFilter, type DayLogFilter, type Preset } from "../dayLog";
import { useDayLedger } from "../queries";
import { PollDepsContext, useConsolePeers } from "../scope";
import { DayLog } from "./DayLog";

const PRESETS: { value: Preset; label: string }[] = [
  { value: "highlights", label: "Highlights" },
  { value: "everything", label: "Everything" },
  { value: "manual", label: "Manual" },
];

const COUNT = "ml-auto font-mono text-[12px] tabular-nums text-ink-3";

function Box({ name, checked, mixed = false, onChange, children, count, head = false }: {
  name: string; checked: boolean; mixed?: boolean; onChange: (next: boolean) => void; children: ReactNode; count: number; head?: boolean;
}) {
  return (
    <label className={`flex min-h-8 cursor-pointer items-center gap-3 ${head ? "font-semibold text-ink" : "pl-7 text-ink-2"}`}>
      <input type="checkbox" aria-label={name} checked={checked} onChange={() => onChange(mixed || !checked)}
        ref={(input) => { if (input) input.indeterminate = mixed; }}
        className="size-3.5 shrink-0 cursor-pointer accent-act" />
      {children}
      <span className={COUNT}>{count}</span>
    </label>
  );
}

function tally(entries: LedgerEntry[]) {
  const items = new Map<string, number>();
  const projects = new Map<string, number>();
  for (const entry of entries) {
    const { item } = categoryOf(entry);
    items.set(item, (items.get(item) ?? 0) + 1);
    if (entry.project != null) {
      const name = projectName(entry.project);
      projects.set(name, (projects.get(name) ?? 0) + 1);
    }
  }
  return { items, projects };
}

/** Settings › Today's log: a preset and, per group, the items and projects
 *  the Overview's log keeps, saved at the origin daemon on each change,
 *  beside a preview of the log they keep. */
export function DayLogTab() {
  const saved = useDayLogFilter();
  const [chosen, setChosen] = useState<DayLogFilter | null>(null);
  const filter = chosen ?? saved.filter;
  const setFilter = (next: DayLogFilter) => {
    setChosen(next);
    saved.setFilter(next);
  };
  const { hosts, now } = useConsolePeers();
  const deps = useContext(PollDepsContext);
  const { entries } = useDayLedger(hosts, now, deps);
  const counts = tally(entries);
  const colours = projectColours(hosts);
  const items = itemsOf(filter);
  const manual = (change: Partial<Pick<DayLogFilter, "items" | "projects">>) => setFilter({
    preset: "manual",
    items: { ...items, ...change.items },
    projects: { ...(filter.preset === "manual" ? filter.projects : {}), ...change.projects },
  });
  const pick = (preset: Preset) => {
    if (preset === filter.preset) return;
    setFilter(preset === "highlights" ? HIGHLIGHTS : preset === "everything" ? EVERYTHING : { ...filter, preset: "manual", items });
  };
  return (
    <div className="mt-5 grid gap-6 min-[1100px]:grid-cols-[minmax(0,1fr)_280px]">
    <div className="min-w-0">
      <div className="flex flex-wrap items-center justify-end gap-3">
        <span id="day-log-preset" className="font-sans text-[14px] text-ink">Preset</span>
        <div role="group" aria-labelledby="day-log-preset" className="inline-flex border border-rule">
          {PRESETS.map((preset) => (
            <button key={preset.value} type="button" aria-pressed={filter.preset === preset.value} onClick={() => pick(preset.value)}
              className={`min-h-10 px-3.5 font-mono text-[11.5px] uppercase tracking-[0.08em] not-first:border-l not-first:border-rule ${filter.preset === preset.value ? "bg-ink text-paper" : "bg-paper text-ink-2 hover:bg-paper-2"}`}>
              {preset.label}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-5 border border-rule bg-paper text-[14px]">
        {LOG_GROUPS.map((group) => {
          const on = group.items.filter((each) => items[each.id]).length;
          const total = group.items.reduce((sum, each) => sum + (counts.items.get(each.id) ?? 0), 0);
          return (
            <section key={group.id} aria-label={group.label} className="border-rule px-4 py-2.5 not-first:border-t">
              <Box head name={group.label} checked={on === group.items.length} mixed={on > 0 && on < group.items.length} count={total}
                onChange={(next) => manual({ items: Object.fromEntries(group.items.map((each) => [each.id, next])) })}>
                {group.label}
              </Box>
              {group.items.map((each) => (
                <Box key={each.id} name={each.label} checked={items[each.id] === true} count={counts.items.get(each.id) ?? 0}
                  onChange={(next) => manual({ items: { [each.id]: next } })}>
                  {each.label}
                </Box>
              ))}
            </section>
          );
        })}
        <section aria-label="Projects" className="border-t border-rule px-4 py-2.5">
          <h3 className="flex min-h-8 items-center pl-7 font-semibold text-ink">Projects</h3>
          {[...colours].map(([name, colour]) => (
            <Box key={name} name={name} checked={projectOn(filter, name)} count={counts.projects.get(name) ?? 0}
              onChange={(next) => manual({ projects: { [name]: next } })}>
              <span aria-hidden="true" className="size-2 shrink-0" style={{ backgroundColor: colour }} />
              <span className="font-mono text-[13px]">{name}</span>
            </Box>
          ))}
        </section>
      </div>
    </div>
    <DayLog entries={entries} preview={filter} className="min-[1100px]:sticky min-[1100px]:top-4 min-[1100px]:h-auto min-[1100px]:max-h-[calc(100vh-2rem)]" />
    </div>
  );
}
