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
  "flex items-center gap-2.5 font-sans text-[11px] font-semibold uppercase leading-none tracking-[.06em] text-ink-3 after:flex-1 after:border-t after:border-rule-2 after:content-['']";

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

/** A ticket's acceptance criteria with their met count; `keys` adds the
 *  AC1, AC2… column and `run` names the run the count is from. */
export function Criteria({ rows, keys = false, run = null }: { rows: CriterionRow[]; keys?: boolean; run?: number | null }) {
  const met = rows.filter((row) => row.standing === "met").length;
  return (
    <section aria-label="Acceptance criteria">
      <div className={SECTION_LABEL}>
        Acceptance criteria
        <span data-met-count className="tracking-[.04em] text-ink">
          {met} of {rows.length} met{run == null ? "" : ` in run ${run}`}
        </span>
      </div>
      <ul className="m-0 mt-2 list-none p-0">
        {rows.map((row) => (
          <li key={row.n} className="flex items-start gap-2.5 border-b border-rule-2 py-[7px] font-sans text-[13.5px] leading-[1.4] text-ink">
            {keys && <span className="mt-[3px] w-[34px] shrink-0 font-mono text-[11px] font-semibold leading-none text-ink-3">AC{row.n}</span>}
            <Mark standing={row.standing} />
            <span className="min-w-0 [overflow-wrap:anywhere]">{row.text}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
