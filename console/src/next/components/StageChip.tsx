import { phaseLabel } from "../../lib/runs";
import { StateDot } from "./StateDot";

export interface StageRun {
  phase: string;
  round?: number;
  max_rounds?: number;
  pr_url?: string | null;
}

/** "round 2 of 3" while a run is reviewing, else null. */
export function reviewRound(run: StageRun): string | null {
  if (run.phase !== "reviewing" || run.round == null) return null;
  return `round ${run.round}${run.max_rounds != null ? ` of ${run.max_rounds}` : ""}`;
}

export function StageChip({ run, note }: { run: StageRun; note?: string | null }) {
  const word = phaseLabel(run.phase, run.pr_url, note);
  const round = reviewRound(run);
  return (
    <span className="inline-flex items-center gap-1.5 border border-rule-2 bg-paper px-2 py-0.5 font-sans text-[13px] text-ink-2">
      <StateDot state="running" hideWord />
      <span>{word}{round == null ? "" : ` · ${round}`}</span>
    </span>
  );
}
