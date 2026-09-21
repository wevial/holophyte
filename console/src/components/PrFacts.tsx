import type { Fact, Tone } from "../lib/attention";

/** A fact chip's classes by tone: the theme's ok, warn and bad washes;
 *  a neutral chip is outlined and faint. */
const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-bg text-ok-text",
  warn: "bg-warn-bg text-warn-text",
  bad: "bg-bad-bg text-bad-text",
  neutral: "border border-chip-border text-faint",
};

export function PrFacts({ facts }: { facts?: Fact[] }) {
  if (!facts?.length) return null;
  return (
    <p data-facts className="mt-1 flex flex-wrap gap-1.5">
      {facts.map((fact) => (
        <span
          key={fact.label}
          data-fact
          data-tone={fact.tone}
          className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${TONE_CLASS[fact.tone]}`}
        >
          {fact.label}
        </span>
      ))}
    </p>
  );
}
