import { useLayoutEffect, useRef, useState } from "react";
import type { RunDetail } from "../../lib/contract.gen";
import { formatSpan, formatTotal } from "../../lib/format";
import {
  barLength,
  buildTimeline,
  centres,
  fitWidths,
  segmentDescription,
  shortLabel,
  spacing,
  totalsLine,
  type SegmentKind,
} from "../../lib/timeline";

const FILLS: Record<SegmentKind, string> = {
  implement: "bg-act text-paper",
  verify: "bg-live text-paper",
  review: "bg-wait text-paper",
  fix: "bg-ask text-paper",
  merge: "bg-ink-2 text-paper",
  wait: "bg-rule text-ink",
  parked: "bg-fact-warn text-paper",
};

/** Where the run's time went: one segment per phase, each as wide as its
 *  share, a tooltip naming it on hover and focus, the running one pulsing,
 *  and each kind's total beneath. */
export function PhaseBar({ detail, now }: { detail: RunDetail; now: number }) {
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
  const earlier = detail.chain?.runs.slice(0, -1) ?? [];
  const segments = buildTimeline(
    { ...detail.run, rounds: detail.rounds, events: detail.events, round_offset: earlier.reduce((sum, entry) => sum + entry.rounds.length, 0) },
    now,
  );
  if (segments.length === 0) return null;
  const { minPx, gapPx } = spacing(segments.length, barPx);
  const widths = fitWidths(segments.map((segment) => segment.width), barPx, Math.max(0, segments.length - 1) * gapPx, minPx);
  const middles = centres(widths, gapPx);
  const hovered = active == null ? undefined : segments[active];
  return (
    <div data-phase-bar className="relative mt-3">
      <ol ref={barRef} aria-label="Phase timeline" className="m-0 flex list-none p-0" style={{ gap: `${gapPx}px` }}>
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
            style={{ width: barLength(widths[index]!) }}
          >
            <span
              aria-hidden="true"
              className={`block h-[22px] overflow-hidden whitespace-nowrap text-center font-mono text-[10.5px] leading-[22px] ${FILLS[segment.kind]} ${segment.running ? "phase-running" : ""}`}
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
          className="pointer-events-none absolute z-10 -translate-x-1/2 whitespace-nowrap border border-rule bg-paper-2 px-2 py-[3px] font-mono text-[11px] text-ink shadow-card"
          style={{ left: barLength(middles[active!]!), bottom: "calc(100% + 4px)" }}
        >
          {segmentDescription(hovered)}
        </div>
      )}
      <p data-phase-totals className="m-0 mt-1.5 font-mono text-[11.5px] text-ink-3">
        {totalsLine(segments)}
      </p>
    </div>
  );
}
