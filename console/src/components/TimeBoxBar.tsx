import { formatAge, formatDuration } from "../lib/format";
import { boxPercent, boxTone } from "../lib/runs";

const FILLS = { teal: "bg-accent", amber: "bg-warn", red: "bg-bad" };

/** Elapsed against the time box: a 6px bar that turns amber at 70 % and
 *  red at 100 %, with `ELAPSED / BOX` beneath. */
export function TimeBoxBar({ elapsedMs, boxMs }: { elapsedMs: number; boxMs: number }) {
  const percent = boxPercent(elapsedMs, boxMs);
  const tone = boxTone(percent);
  return (
    <span className="block min-w-0">
      <span
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        data-tone={tone}
        className="block h-[6px] w-full overflow-hidden rounded-chip bg-track"
      >
        <span className={`block h-full ${FILLS[tone]}`} style={{ width: `${percent}%` }} />
      </span>
      <span className="mt-1 block font-mono text-[12px] text-muted">
        {formatDuration(elapsedMs)} / {formatAge(boxMs)}
      </span>
    </span>
  );
}
