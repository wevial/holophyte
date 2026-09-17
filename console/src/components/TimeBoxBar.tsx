import { formatAge, formatDuration } from "../lib/format";
import { boxPercent, boxTone } from "../lib/runs";

const FILLS = { teal: "bg-accent", amber: "bg-warn", red: "bg-bad" };

/** The bare track: elapsed as a share of the box, teal below 70 %, amber
 *  from 70 %, red at 100 %. The Floor's rows draw it 6px, the Board's
 *  cards 5px; the thresholds are `boxTone`'s either way. */
export function BoxBar({ elapsedMs, boxMs, height = 6 }: { elapsedMs: number | null; boxMs: number; height?: 5 | 6 }) {
  const percent = elapsedMs == null ? null : boxPercent(elapsedMs, boxMs);
  const tone = boxTone(percent ?? 0);
  return (
    <span
      role="progressbar"
      aria-valuenow={percent == null ? undefined : Math.round(percent)}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label={elapsedMs == null ? "Working time unmeasured" : "Working time budget"}
      data-tone={elapsedMs == null ? "none" : tone}
      className={`block ${height === 5 ? "h-[5px]" : "h-[6px]"} w-full overflow-hidden rounded-chip bg-track`}
    >
      <span className={`block h-full ${FILLS[tone]}`} style={{ width: `${percent ?? 0}%` }} />
    </span>
  );
}

/** Elapsed against the time box: a 6px bar that turns amber at 70 % and
 *  red at 100 %, with `ELAPSED / BOX` beneath. */
export function TimeBoxBar({ elapsedMs, boxMs }: { elapsedMs: number | null; boxMs: number }) {
  return (
    <span className="block min-w-0">
      <BoxBar elapsedMs={elapsedMs} boxMs={boxMs} />
      <span className="mt-1 block font-mono text-[12px] text-muted">
        working {elapsedMs == null ? "n/a" : formatDuration(elapsedMs)} / {formatAge(boxMs)}
      </span>
    </span>
  );
}
