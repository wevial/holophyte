import { useState } from "react";
import { formatSpan, formatTotal } from "../lib/format";
import { phaseLabel } from "../lib/runs";
import { segmentName, type Segment, type SegmentKind, type TimelineRun } from "../lib/timeline";

const FILLS: Record<SegmentKind, string> = {
  implement: "bg-accent",
  review: "bg-review",
  fix: "bg-[color-mix(in_oklab,var(--accent),var(--rail-fg)_45%)]",
  verify: "bg-ok",
  merge: "bg-ok-text",
  wait: "bg-faint",
  parked: "bg-warn",
};

/** Gap between bar items, in px; each item gives up its share of the
 *  gaps so that items plus gaps sum to exactly the bar's width. */
export const GAP_PX = 3;

const width = (share: number, gapsPx: number) =>
  `calc(${Math.max(0, share * 100)}% - ${Math.max(0, share) * gapsPx}px)`;

const shortLabel = (segment: Segment) =>
  segment.round != null ? segmentName(segment) : segment.kind === "fix" && segment.reason !== "fix" ? segment.label.replace(/^fix/, "rework") : segment.label;

const description = (segment: Segment) =>
  `${segmentName(segment)} · ${formatTotal(segment.to - segment.from)}${segment.reason ? ` · ${segment.reason}` : ""}`;

/** Proportional segments with share-based labels, reason tooltips and totals.
 * The remainder is unused time in the box; a running segment pulses. */
export function RoundTimeline({
  segments,
  run,
  now,
}: {
  segments: Segment[];
  /** The run the segments came from: `ended_ms` decides "done" (a closed
   *  last segment alone is only a park) and the done figure is its whole
   *  span, not the stretch the segments cover — drawn even when the run's
   *  events left no segment at all. */
  run: Pick<TimelineRun, "started_ms" | "ended_ms" | "phase" | "pr_url">;
  /** The caller's clock; a parked phase's wait ages by it. */
  now: number;
}) {
  const [active, setActive] = useState<number | null>(null);
  const spent = segments.reduce((sum, segment) => sum + segment.width, 0);
  const remainder = Math.max(0, 1 - spent);
  const items = segments.length + (remainder > 0 ? 1 : 0);
  const gapsPx = Math.max(0, items - 1) * GAP_PX;
  const starts: number[] = [];
  let cursor = 0;
  for (const segment of segments) {
    starts.push(cursor);
    cursor += segment.width;
  }
  const totals = new Map<SegmentKind, number>();
  for (const segment of segments) {
    totals.set(segment.kind, (totals.get(segment.kind) ?? 0) + segment.to - segment.from);
  }
  const last = segments[segments.length - 1];
  const status =
    run.ended_ms != null
      ? { label: "done", ms: run.ended_ms - run.started_ms }
      : last == null
        ? undefined
        : last.running
          ? { label: last.kind === "parked" ? phaseLabel(run.phase, run.pr_url) : last.round != null ? segmentName(last) : last.label, ms: last.to - last.from }
          : { label: phaseLabel(run.phase, run.pr_url), ms: now - last.to };
  const hovered = active == null ? undefined : segments[active];
  return (
    <div data-timeline className="relative">
      <ol aria-label="Round timeline" className="flex" style={{ gap: `${GAP_PX}px` }}>
        {segments.map((segment, index) => (
          <li
            key={index}
            data-segment={segment.kind}
            data-running={segment.running ? "true" : undefined}
            role="img"
            tabIndex={0}
            aria-label={`${shortLabel(segment)} ${formatSpan(segment.to - segment.from)}`}
            onMouseEnter={() => setActive(index)}
            onMouseLeave={() => setActive(null)}
            onFocus={() => setActive(index)}
            onBlur={() => setActive(null)}
            className="min-w-0 shrink-0"
            style={{ width: width(segment.width, gapsPx) }}
          >
            <span
              aria-hidden="true"
              title={description(segment)}
              className={`block overflow-hidden whitespace-nowrap h-[22px] rounded-[6px] text-center font-mono text-[10px] leading-[22px] text-badge-text ${FILLS[segment.kind]} ${segment.running ? "segment-running" : ""}`}
            >
              {segment.width >= 0.15 ? `${shortLabel(segment)} · ${formatTotal(segment.to - segment.from)}` : null}
            </span>
          </li>
        ))}
        {remainder > 0 && (
          <li aria-hidden="true" data-segment="remaining" className="min-w-0 shrink-0" style={{ width: width(remainder, gapsPx) }}>
            <span className="block h-[22px] rounded-[6px] bg-well" />
          </li>
        )}
      </ol>
      {hovered && (
        <div
          data-segment-tooltip
          role="tooltip"
          className="pointer-events-none absolute -translate-x-1/2 whitespace-nowrap rounded-button border border-line bg-rail px-2 py-[3px] font-mono text-[11px] text-rail-fg shadow-card"
          style={{ left: `${(starts[active!]! + hovered.width / 2) * 100}%`, bottom: "calc(100% + 4px)" }}
        >
          {description(hovered)}
        </div>
      )}
      {totals.size > 0 && (
        <p data-timeline-totals className="mt-1.5 font-mono text-[11px] text-muted">
          {[...totals].map(([kind, ms]) => `${kind === "fix" ? "rework" : kind} · ${formatTotal(ms)}`).join(" · ")}
        </p>
      )}
      {status && (
        <p data-timeline-status className="mt-1.5 text-[12px]">
          <span className="font-semibold text-ink">{status.label}</span>
          <span className="font-mono text-[11px] text-faint">{` · ${formatTotal(status.ms)}`}</span>
        </p>
      )}
    </div>
  );
}
