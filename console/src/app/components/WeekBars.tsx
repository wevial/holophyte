import { useState } from "react";
import type { ShippedRow } from "../../lib/types";

const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const BAR_PX = 64;


interface Day {
  name: string;
  total: number;
  /** Each project with merges that day, in project order, with its count. */
  projects: [string, number][];
}

const weekday = (ms: number) => (new Date(ms).getDay() + 6) % 7;

function daysOf(rows: ShippedRow[], order: string[]): Day[] {
  const counts = DAY_NAMES.map(() => new Map<string, number>());
  for (const row of rows) {
    const day = counts[weekday(row.ended_ms)]!;
    day.set(row.project, (day.get(row.project) ?? 0) + 1);
  }
  return DAY_NAMES.map((name, index) => {
    const day = counts[index]!;
    const named = [...order.filter((project) => day.has(project)), ...[...day.keys()].filter((project) => !order.includes(project))];
    const projects = named.map((project): [string, number] => [project, day.get(project)!]);
    return { name, total: projects.reduce((sum, [, count]) => sum + count, 0), projects };
  });
}

function labelOf(day: Day): string {
  const parts = day.projects.map(([project, count]) => `${project} ${count}`).join(", ");
  return `${day.name}, ${day.total} merged${parts ? `: ${parts}` : ""}`;
}

function DayCard({ day, colours }: { day: Day; colours: Map<string, string> }) {
  return (
    <span aria-hidden="true" className="absolute top-full left-1/2 z-20 mt-1.5 flex w-max -translate-x-1/2 flex-col gap-1 border border-rule bg-paper px-3 py-2 text-left font-sans text-[13px] leading-[1.45] text-ink shadow-[var(--card-shadow)]">
      <span className="font-semibold">{day.name} <span className="font-mono text-[12px] font-normal text-ink-3">{day.total} merged</span></span>
      {day.projects.map(([project, count]) => (
        <span key={project} className="flex items-center gap-2">
          <span className="block size-1.5 rounded-full bg-ink-3" style={{ background: colours.get(project) }} />
          <span className="mr-auto">{project}</span>
          <span className="pl-3 font-mono text-[12px] text-ink-2">{count}</span>
        </span>
      ))}
    </span>
  );
}

/** This week's merges, Monday to Sunday in local time: each day's bar
 *  scaled to the busiest day and stacked by project in `colours`' order. */
export function WeekBars({ rows, now, colours }: { rows: ShippedRow[]; now: number; colours: Map<string, string> }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const [focused, setFocused] = useState<number | null>(null);
  const days = daysOf(rows, [...colours.keys()]);
  const today = weekday(now);
  const busiest = Math.max(...days.map((day) => day.total));
  const week = days.reduce((sum, day) => sum + day.total, 0);
  return (
    <div data-week className="flex flex-col gap-3">
      <div className="flex min-h-8 items-baseline gap-2">
        <h2 className="font-sans text-[14px] font-semibold text-ink">This week</h2>
        <span data-week-total className="font-mono text-[12px] text-ink-3">{week} merged</span>
      </div>
      <ol className="grid grid-cols-7">
        {days.map((day, index) => {
          const drawn = index <= today && day.total > 0;
          return (
            <li key={day.name} className="flex">
              <button type="button" aria-label={labelOf(day)} data-day={day.name}
                onMouseEnter={() => setHovered(index)} onMouseLeave={() => setHovered(null)}
                onFocus={() => setFocused(index)} onBlur={() => setFocused(null)}
                className="relative flex flex-1 cursor-default flex-col items-center gap-1.5 font-mono text-[11px] text-ink-3">
                <span className="h-4 leading-4 text-ink-2">{index === today ? day.total : ""}</span>
                <span className="flex h-16 items-end">
                  {drawn ? (
                    <span data-bar className="flex w-2.5 flex-col-reverse rounded-[2px]" style={{ height: `${(day.total / busiest) * BAR_PX}px`, overflow: "hidden" }}>
                      {day.projects.map(([project, count]) => (
                        <span key={project} data-segment={project} className="bg-ink-3 not-first:shadow-[inset_0_-1px_0_var(--canvas)]" style={{ flex: `${count} 1 0px`, background: colours.get(project) }} />
                      ))}
                    </span>
                  ) : (
                    <span data-stub className="block h-[2px] w-2.5 bg-rule" />
                  )}
                </span>
                <span data-day-letter className={index === today ? "font-semibold text-ink" : ""}>{day.name[0]}</span>
                {(hovered ?? focused) === index && <DayCard day={day} colours={colours} />}
              </button>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
