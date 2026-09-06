import type { Severity } from "../lib/findings";

const TONES: Record<Severity, string> = {
  must: "bg-bad-bg text-bad-text",
  should: "bg-warn-bg text-warn-text",
  nit: "bg-well text-muted",
};

/** A finding's severity: must on the bad wash, should on warn, nit neutral. */
export function SeverityPill({ severity }: { severity: Severity }) {
  return (
    <span
      data-severity={severity}
      className={`inline-block shrink-0 rounded-pill px-2 py-[3px] font-mono text-[11px] font-semibold ${TONES[severity]}`}
    >
      {severity}
    </span>
  );
}
