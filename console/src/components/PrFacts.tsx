import { Fragment } from "react";
import type { Fact, Tone } from "../lib/attention";

/** A fact chip's classes by tone: the theme's ok, warn and bad washes;
 *  a neutral chip is outlined and faint. */
const TONE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-bg text-ok-text",
  warn: "bg-warn-bg text-warn-text",
  bad: "bg-bad-bg text-bad-text",
  neutral: "border border-chip-border text-faint",
};

const SQUARE_CLASS: Record<Tone, string> = {
  ok: "bg-ok-text",
  warn: "bg-warn-text",
  bad: "bg-bad-text",
  neutral: "bg-faint",
};

/** `chips` as washed pills; `line` as one mono line, each fact a square in its tone and its word. */
export function PrFacts({ facts, form = "chips" }: { facts?: Fact[]; form?: "chips" | "line" }) {
  if (!facts?.length) return null;
  if (form === "line") {
    return (
      <span data-facts className="block font-mono text-[12px] leading-[1.4] text-ink-3">
        {facts.map((fact, index) => (
          <Fragment key={fact.label}>
            {index > 0 && " "}
            <span data-fact data-tone={fact.tone} className="whitespace-nowrap">
              <i aria-hidden="true" className={`mr-1.5 inline-block size-[7px] align-[1px] ${SQUARE_CLASS[fact.tone]}`} />
              {fact.label}
              {index < facts.length - 1 && " ·"}
            </span>
          </Fragment>
        ))}
      </span>
    );
  }
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
