import { phaseLabel, phaseTone } from "../lib/runs";

const TONES = {
  implementing: "bg-phase-implementing-bg text-phase-implementing-text",
  reviewing: "bg-phase-reviewing-bg text-phase-reviewing-text",
  verifying: "bg-phase-verifying-bg text-phase-verifying-text",
  neutral: "bg-well text-muted",
};

/** A run's phase folded into its working word; an unknown phase keeps its
 *  own name on the neutral wash. */
export function PhasePill({ phase, pr_url, note }: { phase: string; pr_url?: string | null; note?: string | null }) {
  const label = phaseLabel(phase, pr_url, note);
  const tone = phaseTone(label);
  return (
    <span
      title={note ?? undefined}
      data-phase={tone}
      className={`inline-block rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${TONES[tone]}`}
    >
      {label}
    </span>
  );
}
