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

const GLYPH_CLASS: Record<Tone, string> = {
  ok: "text-ok-text",
  warn: "text-warn-text",
  bad: "text-bad-text",
  neutral: "text-faint",
};

const GLYPH_PATH: Record<Tone, string> = {
  ok: "M2 6 5 9 10 3",
  warn: "M6 1.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 1 1 0-9",
  bad: "M3 3 9 9M9 3 3 9",
  neutral: "M3 6h6",
};

/** `chips` as washed pills; `line` as one mono line, each fact a glyph in its tone and its word. */
export function PrFacts({ facts, form = "chips" }: { facts?: Fact[]; form?: "chips" | "line" }) {
  if (!facts?.length) return null;
  if (form === "line") {
    return (
      <span data-facts className="block font-mono text-[12px] leading-[1.4] text-ink-3">
        {facts.map((fact, index) => (
          <Fragment key={fact.label}>
            {index > 0 && " "}
            <span data-fact data-tone={fact.tone} className="whitespace-nowrap">
              <svg
                aria-hidden="true"
                data-glyph={fact.tone}
                viewBox="0 0 12 12"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
                className={`mr-1.5 inline-block size-[9px] align-[1px] ${GLYPH_CLASS[fact.tone]}`}
              >
                <path d={GLYPH_PATH[fact.tone]} strokeDasharray={fact.tone === "warn" ? "0.1 3.04" : undefined} />
              </svg>
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
