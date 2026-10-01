import { useLayoutEffect, useRef, useState } from "react";
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

/** The narrowest a segment draws, so it stays hoverable and focusable. */
const MIN_SEGMENT_PX = 6;

const width = (share: number, gapsPx: number) =>
  `calc(${Math.max(0, share * 100)}% - ${Math.max(0, share) * gapsPx}px)`;

/** Each segment's CSS width in a bar `barPx` wide: a share that would draw
 *  under the minimum takes the minimum and the rest split what is left by
 *  share. An unmeasured bar draws plain shares. */
function fitWidths(shares: number[], barPx: number, gapsPx: number): string[] {
  const pinned = new Set<number>();
  for (let grew = barPx > 0; grew; ) {
    const free = shares.reduce((sum, share, index) => (pinned.has(index) ? sum : sum + share), 0);
    const room = barPx - gapsPx - pinned.size * MIN_SEGMENT_PX;
    grew = false;
    shares.forEach((share, index) => {
      if (pinned.has(index) || (free > 0 && (share / free) * room >= MIN_SEGMENT_PX)) return;
      pinned.add(index);
      grew = true;
    });
  }
  const free = shares.reduce((sum, share, index) => (pinned.has(index) ? sum : sum + share), 0);
  const reserved = gapsPx + pinned.size * MIN_SEGMENT_PX;
  return shares.map((share, index) => (pinned.has(index) ? `${MIN_SEGMENT_PX}px` : width(free > 0 ? share / free : 0, reserved)));
}

const shortLabel = (segment: Segment) =>
  segment.round != null ? segmentName(segment) : segment.kind === "fix" && segment.reason !== "fix" ? segment.label.replace(/^fix/, "rework") : segment.label;

const description = (segment: Segment) =>
  `${segmentName(segment)} · ${formatTotal(segment.to - segment.from)}${segment.reason ? ` · ${segment.reason}` : ""}`;

/** Segments filling the bar by their share of the time spent, with
 * share-based labels, reason tooltips and totals; a running segment pulses. */
export function RoundTimeline({
  segments,
  run,
  now,
  caption,
}: {
  segments: Segment[];
  /** The run the segments came from: `ended_ms` decides "done" (a closed
   *  last segment alone is only a park) and the done figure is its whole
   *  span, not the stretch the segments cover — drawn even when the run's
   *  events left no segment at all. */
  run: Pick<TimelineRun, "started_ms" | "ended_ms" | "phase" | "pr_url">;
  /** The caller's clock; a parked phase's wait ages by it. */
  now: number;
  /** Names the run the bar draws when its card shows more than one. */
  caption?: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const [barPx, setBarPx] = useState(0);
  const barRef = useRef<HTMLOListElement>(null);
  useLayoutEffect(() => {
    const element = barRef.current;
    if (!element) return;
    const measure = () => setBarPx(element.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const gapsPx = Math.max(0, segments.length - 1) * GAP_PX;
  const widths = fitWidths(segments.map((segment) => segment.width), barPx, gapsPx);
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
      {caption && <p data-timeline-caption className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">{caption}</p>}
      <ol ref={barRef} aria-label="Round timeline" className="flex" style={{ gap: `${GAP_PX}px` }}>
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
            style={{ width: widths[index] }}
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
