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

/** Legend entries shorter than this are noise (a phase entered and left
 *  in the same second); the bar still draws them at their true width. */
export const LEGEND_MIN_MS = 1000;

/** The run's phases as a 22px bar, each segment as wide as its share of
 *  the time box; the box's unspent remainder is the empty track, and the
 *  running segment pulses, and each segment names itself on hover. The
 *  labels are a legend beneath the bar, in order, each a colour dot with
 *  the phase and its duration: they wrap as a list and never try to sit
 *  under their segment, since a label is wider than a short phase and
 *  cells that stretched to fit pushed every later label off its bar. */
export function RoundTimeline({ segments }: { segments: Segment[] }) {
  const spent = segments.reduce((sum, segment) => sum + segment.width, 0);
  const remainder = Math.max(0, 1 - spent);
  const items = segments.length + (remainder > 0 ? 1 : 0);
  const gapsPx = Math.max(0, items - 1) * GAP_PX;
  return (
    <div data-timeline>
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
        <ol aria-hidden="true" data-segment-labels className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
          {segments
            .filter((segment) => segment.to - segment.from >= LEGEND_MIN_MS)
            .map((segment, index) => (
              <li key={index} data-segment-label={segment.kind} className="flex items-baseline gap-1.5 whitespace-nowrap">
                <span aria-hidden="true" className={`inline-block h-[8px] w-[8px] rounded-[2px] ${FILLS[segment.kind]}`} />
                <span className="text-[12px] font-semibold text-ink">{segment.label}</span>
                <span className="font-mono text-[11px] text-faint">{formatSpan(segment.to - segment.from)}</span>
              </li>
            ))}
        </ol>
      )}
    </div>
  );
}
