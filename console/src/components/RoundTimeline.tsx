import { useEffect, useRef, useState } from "react";
import { formatSpan } from "../lib/format";
import type { Segment, SegmentKind } from "../lib/timeline";

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

/** A segment whose slice of the measured bar is narrower than this shows
 *  no label beneath it; its hover title still names it. */
export const LABEL_MIN_PX = 72;

/** Before the bar's first measure, a segment wider than this share of
 *  the bar still carries its label. */
const UNSIZED_MIN_SHARE = 0.15;

/** Whether `share` of a `barPx`-wide bar is wide enough for the
 *  segment's label; unmeasured (`null`), the share rule stands in. */
export function labelFits(share: number, barPx: number | null): boolean {
  return barPx == null ? share > UNSIZED_MIN_SHARE : share * barPx >= LABEL_MIN_PX;
}

/** The run's phases as a 22px bar, each segment as wide as its share of
 *  the time box; the box's unspent remainder is the empty track, the
 *  running segment pulses, and each segment names itself on hover. Each
 *  label sits directly under its segment's left edge — the phase and its
 *  duration, no colour dot, since it sits under its colour — and a
 *  segment too narrow for a label shows none. A `ResizeObserver` on the
 *  container measures the bar so labels appear and disappear as the card
 *  resizes; `barPx` pins the measure for tests. */
export function RoundTimeline({ segments, barPx }: { segments: Segment[]; barPx?: number }) {
  const container = useRef<HTMLDivElement>(null);
  const [measured, setMeasured] = useState<number | null>(null);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => setMeasured(entries[0]!.contentRect.width));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const bar = barPx ?? measured;
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
  return (
    <div data-timeline ref={container}>
      <ol aria-label="Round timeline" className="flex" style={{ gap: `${GAP_PX}px` }}>
        {segments.map((segment, index) => (
          <li
            key={index}
            data-segment={segment.kind}
            data-running={segment.running ? "true" : undefined}
            aria-label={`${segment.label} ${formatSpan(segment.to - segment.from)}`}
            title={`${segment.label} · ${formatSpan(segment.to - segment.from)}`}
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
      {segments.length > 0 && (
        <ol aria-hidden="true" data-segment-labels className="relative mt-1.5 h-[18px]">
          {segments.map((segment, index) =>
            labelFits(segment.width, bar) ? (
              <li
                key={index}
                data-segment-label={segment.kind}
                className="absolute flex items-baseline gap-1.5 whitespace-nowrap"
                style={{ left: `${starts[index]! * 100}%` }}
              >
                <span className="text-[12px] font-semibold text-ink">{segment.label}</span>
                <span className="font-mono text-[11px] text-faint">{formatSpan(segment.to - segment.from)}</span>
              </li>
            ) : null,
          )}
        </ol>
      )}
    </div>
  );
}
