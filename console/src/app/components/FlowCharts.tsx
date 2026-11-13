import { useState, type ReactNode } from "react";
import { weekLabel, weeklyChange, weekRange, type Better, type Tip } from "../flow";

const STEPS = [1, 2, 5, 10];
const BAR_SHARE = 0.6;
const ACTIVE_BAR_SHARE = 0.75;

type Point = [number, number];

/** The smallest 1, 2 or 5 times a power of ten at or above `max`, never below 1 so one decimal tells each gridline apart. */
function roundedTop(max: number): number {
  if (!(max > 1)) return 1;
  const power = 10 ** Math.floor(Math.log10(max));
  return (STEPS.find((step) => step * power >= max) ?? 10) * power;
}

function weekX(index: number, count: number): number {
  return ((index + 0.5) / count) * 100;
}

function plotY(value: number, top: number): number {
  return 100 - (Math.min(value, top) / top) * 100;
}

function round(value: number): number {
  return Math.round(value * 100) / 100;
}

function place(index: number, count: number, value: number, top: number) {
  return { left: `${weekX(index, count)}%`, top: `${plotY(value, top)}%` };
}

function segments(values: (number | null)[], top: number): Point[][] {
  const runs: Point[][] = [];
  let current: Point[] = [];
  values.forEach((value, index) => {
    if (value == null) {
      if (current.length > 0) runs.push(current);
      current = [];
    } else current.push([round(weekX(index, values.length)), round(plotY(value, top))]);
  });
  if (current.length > 0) runs.push(current);
  return runs;
}

function smoothPath(points: Point[]): string {
  return points
    .map(([x, y], index) => {
      if (index === 0) return `M${x} ${y}`;
      const [px, py] = points[index - 1]!;
      const middle = round((px + x) / 2);
      return `C${middle} ${py} ${middle} ${y} ${x} ${y}`;
    })
    .join(" ");
}

function areaPath(points: Point[]): string {
  return `${smoothPath(points)} L${points.at(-1)![0]} 100 L${points[0]![0]} 100 Z`;
}

interface ChartProps {
  title: string;
  note: string;
  starts: number[];
  format: (value: number) => string;
  tip: (value: number, index: number) => Tip;
}

function edge(index: number, count: number): string {
  if (index === 0) return "-translate-x-3";
  return index === count - 1 ? "-translate-x-[calc(100%-0.75rem)]" : "-translate-x-1/2";
}

function Tooltip({ range, tip, style, align }: { range: string; tip: Tip; style: ReturnType<typeof place>; align: string }) {
  return (
    <span
      aria-hidden="true"
      data-tooltip
      style={style}
      className={`pointer-events-none absolute z-10 flex -translate-y-[calc(100%+10px)] ${align} flex-col gap-0.5 whitespace-nowrap border border-rule bg-paper px-2.5 py-1.5`}
    >
      <span className="font-mono text-[10.5px] text-ink-3">{range}</span>
      <span className="font-mono text-[12px] font-semibold text-ink">{tip.value}</span>
      <span className="font-sans text-[11.5px] text-ink-2">{tip.context}</span>
    </span>
  );
}

function Chart({ title, note, starts, format, tip, top, values, none, children }: ChartProps & { top: number; values: (number | null)[]; none: string; children: (active: number | null) => ReactNode }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const [focused, setFocused] = useState<number | null>(null);
  const active = hovered ?? focused;
  const ticks = [top, top / 2, 0];
  const count = values.length;
  const spoken = values.map((value, index) => `${weekLabel(starts[index]!)} ${value == null ? none : format(value)}`).join(", ");
  const activeValue = active == null ? null : values[active];
  return (
    <figure aria-label={`${title} ${note}: ${spoken}`} data-chart={title} className="flex flex-col gap-3 bg-paper px-4 pt-3.5 pb-3">
      <figcaption aria-hidden="true" className="flex items-baseline justify-between gap-3">
        <span className="font-sans text-[13px] font-semibold text-ink">{title}</span>
        <span className="font-sans text-[11.5px] text-ink-3">{note}</span>
      </figcaption>
      <div className="grid grid-cols-[2.5rem_1fr] grid-rows-[7.5rem_auto] gap-x-1 gap-y-1.5">
        <div aria-hidden="true" className="relative">
          {ticks.map((value) => (
            <span key={value} style={{ top: `${plotY(value, top)}%` }} className="absolute left-0 -translate-y-1/2 font-mono text-[10.5px] text-ink-3">{format(value)}</span>
          ))}
        </div>
        <div className="relative">
          <div aria-hidden="true" className="contents">
            {ticks.map((value) => (
              <span key={value} data-grid={format(value)} style={{ top: `${plotY(value, top)}%` }} className="absolute inset-x-0 border-t border-dashed border-rule" />
            ))}
            {children(active)}
          </div>
          {values.map((value, index) => {
            if (value == null) return null;
            const { value: said, context } = tip(value, index);
            return (
              <button
                key={index}
                type="button"
                data-point={index}
                aria-label={`${weekRange(starts[index]!)}: ${said}, ${context}`}
                onMouseEnter={() => setHovered(index)}
                onMouseLeave={() => setHovered(null)}
                onFocus={() => setFocused(index)}
                onBlur={() => setFocused(null)}
                style={{ left: `${(index / count) * 100}%`, width: `${100 / count}%` }}
                className="absolute inset-y-0 cursor-default focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus"
              />
            );
          })}
          {active != null && activeValue != null && (
            <Tooltip range={weekRange(starts[active]!)} tip={tip(activeValue, active)} style={place(active, count, activeValue, top)} align={edge(active, count)} />
          )}
        </div>
        <span />
        <div aria-hidden="true" className="flex">
          {starts.map((start) => (
            <span key={start} className="flex-1 text-center font-mono text-[10.5px] text-ink-3">{weekLabel(start)}</span>
          ))}
        </div>
      </div>
    </figure>
  );
}

export function BarChart({ values, ...props }: ChartProps & { values: number[] }) {
  const top = roundedTop(Math.max(0, ...values));
  const latest = values.length - 1;
  return (
    <Chart {...props} top={top} values={values} none="none">
      {(active) => (
        <>
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible">
            {values.map((value, index) => {
              const y = round(plotY(value, top));
              const width = (100 / values.length) * (index === active ? ACTIVE_BAR_SHARE : BAR_SHARE);
              return (
                <rect
                  key={index}
                  x={round(weekX(index, values.length) - width / 2)}
                  y={y}
                  width={round(width)}
                  height={round(100 - y)}
                  className={index === latest ? "fill-accent" : "fill-wait"}
                />
              );
            })}
          </svg>
          {values.map((value, index) => (
            <span
              key={index}
              style={place(index, values.length, value, top)}
              className={`absolute -translate-x-1/2 -translate-y-[calc(100%+3px)] font-mono text-[10.5px] ${index === latest ? "font-semibold text-ink" : "text-ink-3"} ${index === latest && active != null ? "invisible" : ""}`}
            >
              {props.format(value)}
            </span>
          ))}
        </>
      )}
    </Chart>
  );
}

export function LineChart({ values, top: fixedTop, ...props }: ChartProps & { values: (number | null)[]; top?: number }) {
  const top = fixedTop ?? roundedTop(Math.max(0, ...values.map((value) => value ?? 0)));
  const runs = segments(values, top);
  const latest = values.length - 1;
  const latestValue = values[latest];
  return (
    <Chart {...props} top={top} values={values} none="no data">
      {(active) => (
        <>
          <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible">
            {runs.map((points) => (
              <path key={`fill-${points[0]![0]}`} d={areaPath(points)} className="fill-wait opacity-15" />
            ))}
            {runs.map((points) => (
              <path key={`line-${points[0]![0]}`} data-segment d={smoothPath(points)} fill="none" strokeWidth={1.75} vectorEffect="non-scaling-stroke" className="stroke-wait" />
            ))}
          </svg>
          {values.map((value, index) =>
            value == null ? null : (
              <span
                key={index}
                data-dot={index}
                style={place(index, values.length, value, top)}
                className={`absolute -translate-1/2 rounded-full border-[1.5px] border-wait ${index === active ? "size-3" : "size-2"} ${index === latest ? "bg-wait" : "bg-paper"}`}
              />
            ),
          )}
          {latestValue != null && active == null && (
            <span
              data-latest
              style={place(latest, values.length, latestValue, top)}
              className="absolute -translate-x-full -translate-y-[calc(100%+6px)] font-mono text-[11px] font-semibold text-ink"
            >
              {props.format(latestValue)}
            </span>
          )}
        </>
      )}
    </Chart>
  );
}

export function Sparkline({ values }: { values: (number | null)[] }) {
  const known = values.flatMap((value) => (value == null ? [] : [value]));
  const bottom = Math.min(...known);
  const span = Math.max(...known) - bottom || 1;
  const scaled = values.map((value) => (value == null ? null : value - bottom));
  const latest = scaled.at(-1);
  return (
    <span aria-hidden="true" className="relative block h-6 w-16 shrink-0">
      <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 size-full overflow-visible">
        {segments(scaled, span).map((points) => (
          <path key={points[0]![0]} d={smoothPath(points)} fill="none" strokeWidth={1.5} vectorEffect="non-scaling-stroke" className="stroke-wait" />
        ))}
      </svg>
      {latest != null && (
        <span style={place(scaled.length - 1, scaled.length, latest, span)} className="absolute size-1.5 -translate-1/2 rounded-full bg-wait" />
      )}
    </span>
  );
}

const TRENDS = {
  merged: { better: "more", unit: "" },
  waiting: { better: "fewer", unit: " pts" },
  rounds: { better: "fewer", unit: "" },
  interventions: { better: "fewer", unit: "" },
} as const satisfies Record<string, { better: Better; unit: string }>;

const TONES = { good: "text-live", bad: "text-accent", flat: "text-ink-3" };

export type TrendNumber = keyof typeof TRENDS;

export function Change({ number, values }: { number: TrendNumber; values: (number | null)[] }) {
  const { better, unit } = TRENDS[number];
  const change = weeklyChange(values, better, unit);
  if (change == null) return null;
  return <dd data-change={number} className={`font-mono text-[11.5px] ${TONES[change.tone]}`}>{change.text}</dd>;
}
