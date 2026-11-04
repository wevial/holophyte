import type { ComponentProps } from "react";
import { phaseLabel } from "../../lib/runs";
import type { State } from "../state";
import { StateDot } from "./StateDot";

export interface StageRun {
  phase: string;
  round?: number;
  max_rounds?: number;
  pr_url?: string | null;
}

/** "round 2 of 3" within the cap; "round 3" past it or with no cap. */
export function roundLabel(round: number, max?: number | null): string {
  return max != null && round <= max ? `round ${round} of ${max}` : `round ${round}`;
}

/** The run's round while it is reviewing, else null. */
export function reviewRound(run: StageRun): string | null {
  if (run.phase !== "reviewing" || run.round == null) return null;
  return roundLabel(run.round, run.max_rounds);
}

/** "reviewing · round 2 of 3": a live run's stage, with its round while reviewing. */
export function stageWord(run: StageRun, note?: string | null): string {
  const word = phaseLabel(run.phase, run.pr_url, note);
  const round = reviewRound(run);
  return round == null ? word : `${word} · ${round}`;
}

type StageText = { run: StageRun; note?: string | null } | { label: string };

/** A run's stage behind its state's icon, from its phase or as `label`, with
 *  `sub` after it in the quieter ink. */
export function StageChip(props: StageText & { state?: State; sub?: string | null }) {
  const { state = "running", sub } = props;
  return (
    <span className="inline-flex items-center gap-1.5 border border-rule-2 bg-paper px-2 py-0.5 font-sans text-[13px] text-ink-2">
      <StateDot state={state} hideWord />
      <span>
        {"label" in props ? props.label : stageWord(props.run, props.note)}
        {sub != null && <span className="text-ink-3"> · {sub}</span>}
      </span>
    </span>
  );
}

/** A tab over the rows after it in a lane's list: `label` behind `state`'s
 *  icon, then `sub`, with `children` beside it. */
export function StageTab({ label, state, sub, children, ...item }: { label: string; state: State; sub?: string | null } & ComponentProps<"li">) {
  return (
    <li {...item} className="flex flex-wrap items-end gap-x-3 border-b border-rule px-5 pt-1.5">
      <span className="relative -mb-px border border-b-0 border-rule bg-paper px-2 py-1">
        <StageChip label={label} state={state} sub={sub} />
      </span>
      {children}
    </li>
  );
}
