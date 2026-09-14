import { useState } from "react";
import { formatSpan, formatTotal } from "../lib/format";
import { phaseLabel } from "../lib/runs";
import { segmentName, type Segment, type SegmentKind, type TimelineRun } from "../lib/timeline";

const FILLS: Record<SegmentKind, string> = {
  implement: "bg-accent",
  review: "bg-review",
  fix: "bg-accent",
  verify: "bg-ok",
  merge: "bg-ok",
};

/** Gap between bar items, in px; each item gives up its share of the
 *  gaps so that items plus gaps sum to exactly the bar's width. */
export const GAP_PX = 3;

const width = (share: number, gapsPx: number) =>
  `calc(${Math.max(0, share * 100)}% - ${Math.max(0, share) * gapsPx}px)`;

/** The run's phases as a 22px bar, each segment as wide as its share of
 *  the time box; the box's unspent remainder is the empty track and the
 *  running segment pulses. No per-segment labels: one status line under
 *  the bar names the current phase and its live duration on a running
 *  run, or reads "done" with the run's whole span on a finished one —
 *  the live figure ticks because the caller rebuilds the segments each
 *  second. A live run parked between segments (on the operator or on
 *  merge approval) names its phase and how long it has waited. Hovering
 *  or focusing a segment floats one tooltip above the bar at the
 *  segment's centre, naming the phase and its duration. */
export function RoundTimeline({
  segments,
  run,
  now,
}: {
  segments: Segment[];
  /** The run the segments came from: `ended_ms` decides "done" (a closed
   *  last segment alone is only a park) and the done figure is its whole
   *  span, not the stretch the segments cover. */
  run: Pick<TimelineRun, "started_ms" | "ended_ms" | "phase">;
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
  const last = segments[segments.length - 1];
  const status =
    last == null
      ? undefined
      : run.ended_ms != null
        ? { label: "done", ms: run.ended_ms - run.started_ms }
        : last.running
          ? { label: last.label, ms: last.to - last.from }
          : { label: phaseLabel(run.phase), ms: now - last.to };
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
            aria-label={`${segment.label} ${formatSpan(segment.to - segment.from)}`}
            onMouseEnter={() => setActive(index)}
            onMouseLeave={() => setActive(null)}
            onFocus={() => setActive(index)}
            onBlur={() => setActive(null)}
            className="min-w-0 shrink-0"
            style={{ width: width(segment.width, gapsPx) }}
          >
            <span
              aria-hidden="true"
              className={`block h-[22px] rounded-[6px] ${FILLS[segment.kind]} ${segment.running ? "segment-running" : ""}`}
            />
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
          {segmentName(hovered)} · {formatSpan(hovered.to - hovered.from)}
        </div>
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
