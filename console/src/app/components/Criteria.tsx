import type { CriterionRow, Standing } from "../../lib/criteria";
import type { State, Tone } from "../state";
import { STATE_ICONS, TONE_TEXT } from "./StateDot";

const MARKS: Record<Standing, { icon: State; tone: Tone; name: string }> = {
  met: { icon: "ready_to_merge", tone: "live", name: "met" },
  open: { icon: "queued", tone: "wait", name: "not yet witnessed" },
  unmet: { icon: "failed", tone: "need", name: "unmet, review found a gap" },
  todo: { icon: "backlog", tone: "ink-3", name: "not started" },
};

export const SECTION_LABEL =
  "flex min-h-[30px] items-center gap-2.5 border-b border-hair font-sans text-[13px] font-semibold leading-none text-ink";

function Mark({ standing }: { standing: Standing }) {
  const { icon, tone, name } = MARKS[standing];
  return (
    <svg
      viewBox="0 0 16 16"
      width={14}
      height={14}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label={name}
      data-standing={standing}
      className={`mt-0.5 size-3.5 shrink-0 ${TONE_TEXT[tone]}`}
    >
      {STATE_ICONS[icon]}
    </svg>
  );
}

/** `held` withholds count and standings while the run's are not on hand. */
export function Criteria({
  rows,
  run = null,
  held = null,
}: {
  rows: CriterionRow[];
  run?: number | null;
  held?: "loading" | "unavailable" | null;
}) {
  const met = rows.filter((row) => row.standing === "met").length;
  const count = held === "loading" ? `awaiting run ${run}` : held === "unavailable" ? `run ${run} unavailable` : `${met} of ${rows.length} met${run == null ? "" : ` in run ${run}`}`;
  return (
    <section aria-label="Acceptance criteria">
      <div className={SECTION_LABEL}>
        Acceptance criteria
        <span data-met-count className="font-mono text-[12px] font-normal text-ink-3">
          {count}
        </span>
      </div>
      <ul className="m-0 mt-1 list-none p-0">
        {rows.map((row) => (
          <li key={row.id} className="flex items-start gap-2.5 border-b border-hair py-2 font-sans text-[14px] leading-[1.5] text-ink">
            <strong className="mt-[3px] w-[34px] shrink-0 font-mono text-[11px] font-bold leading-none text-ink-3">{row.id}</strong>
            {held == null ? <Mark standing={row.standing} /> : <span className="size-3.5 shrink-0" />}
            <span className="min-w-0 max-w-[75ch] [overflow-wrap:anywhere]">{row.text}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
