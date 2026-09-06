import { formatSpan } from "../lib/format";
import type { Segment, SegmentKind } from "../lib/timeline";

const FILLS: Record<SegmentKind, string> = {
  implement: "bg-accent",
  review: "bg-review",
  fix: "bg-accent",
  verify: "bg-ok",
};

/** Gap between bar items, in px; each item gives up its share of the
 *  gaps so that items plus gaps sum to exactly the bar's width. */
export const GAP_PX = 3;

const width = (share: number, gapsPx: number) =>
  `calc(${Math.max(0, share * 100)}% - ${Math.max(0, share) * gapsPx}px)`;

/** The run's phases as a 22px bar, each segment as wide as its share of
 *  the time box with its label and duration beneath; the box's unspent
 *  remainder is the empty track, and the running segment pulses. */
export function RoundTimeline({ segments }: { segments: Segment[] }) {
  const spent = segments.reduce((sum, segment) => sum + segment.width, 0);
  const remainder = Math.max(0, 1 - spent);
  const items = segments.length + (remainder > 0 ? 1 : 0);
  const gapsPx = Math.max(0, items - 1) * GAP_PX;
  return (
    <ol aria-label="Round timeline" className="flex" style={{ gap: `${GAP_PX}px` }}>
      {segments.map((segment, index) => (
        <li
          key={index}
          data-segment={segment.kind}
          data-running={segment.running ? "true" : undefined}
          className="min-w-0 shrink-0"
          style={{ width: width(segment.width, gapsPx) }}
        >
          <span
            aria-hidden="true"
            className={`block h-[22px] rounded-[6px] ${FILLS[segment.kind]} ${segment.running ? "segment-running" : ""}`}
          />
          <span className="mt-1 block truncate text-[12px] font-semibold text-ink">{segment.label}</span>
          <span className="block font-mono text-[11px] text-faint">{formatSpan(segment.to - segment.from)}</span>
        </li>
      ))}
      {remainder > 0 && (
        <li aria-hidden="true" data-segment="remaining" className="min-w-0 shrink-0" style={{ width: width(remainder, gapsPx) }}>
          <span className="block h-[22px] rounded-[6px] bg-well" />
        </li>
      )}
    </ol>
  );
}
