import { formatAge, formatDuration } from "../lib/format";
import { boxPercent, boxTone } from "../lib/runs";

const FILLS = { teal: "bg-accent", amber: "bg-warn", red: "bg-bad" };

/** The bare track: elapsed as a share of the box, teal below 70 %, amber
 *  from 70 %, red at 100 %. The Floor's rows draw it 6px, the Board's
 *  cards 5px; the thresholds are `boxTone`'s either way. */
export function BoxBar({ elapsedMs, boxMs, height = 6 }: { elapsedMs: number; boxMs: number; height?: 5 | 6 }) {
  const percent = boxPercent(elapsedMs, boxMs);
  const tone = boxTone(percent);
  return (
    <span
      role="progressbar"
      aria-valuenow={Math.round(percent)}
      aria-valuemin={0}
      aria-valuemax={100}
      data-tone={tone}
      className={`block ${height === 5 ? "h-[5px]" : "h-[6px]"} w-full overflow-hidden rounded-chip bg-track`}
    >
      <span className={`block h-full ${FILLS[tone]}`} style={{ width: `${percent}%` }} />
    </span>
  );
}

/** Elapsed against the time box: a 6px bar that turns amber at 70 % and
 *  red at 100 %, with `ELAPSED / BOX` beneath. */
export function TimeBoxBar({ elapsedMs, boxMs }: { elapsedMs: number; boxMs: number }) {
  return (
    <span className="block min-w-0">
      <BoxBar elapsedMs={elapsedMs} boxMs={boxMs} />
      <span className="mt-1 block font-mono text-[12px] text-muted">
        {formatDuration(elapsedMs)} / {formatAge(boxMs)}
      </span>
    </span>
  );
}
