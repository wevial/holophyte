import { formatSpan } from "../lib/format";
import type { Segment, SegmentKind } from "../lib/timeline";

const FILLS: Record<SegmentKind, string> = {
  implement: "bg-accent",
  review: "bg-review",
  fix: "bg-accent",
  verify: "bg-ok",
};

const percent = (share: number) => `${Math.max(0, share * 100)}%`;

/** The run's phases as a 22px bar, each segment as wide as its share of
 *  the time box with its label and duration beneath; the box's unspent
 *  remainder is the empty track, and the running segment pulses. */
export function RoundTimeline({ segments }: { segments: Segment[] }) {
  const spent = segments.reduce((sum, segment) => sum + segment.width, 0);
  const remainder = Math.max(0, 1 - spent);
  return (
    <ol aria-label="Round timeline" className="flex gap-[3px]">
      {segments.map((segment, index) => (
        <li
          key={index}
          data-segment={segment.kind}
          data-running={segment.running ? "true" : undefined}
          className="min-w-0 shrink-0"
          style={{ width: percent(segment.width) }}
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
        <li aria-hidden="true" data-segment="remaining" className="min-w-0 shrink-0" style={{ width: percent(remainder) }}>
          <span className="block h-[22px] rounded-[6px] bg-well" />
        </li>
      )}
    </ol>
  );
}
