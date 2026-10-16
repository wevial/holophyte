import type { ReactNode } from "react";
import { STATES, type State, type Tone } from "../state";

const TONE_TEXT: Record<Tone, string> = {
  bad: "text-bad",
  ask: "text-ask",
  need: "text-need",
  live: "text-live",
  wait: "text-wait",
  "ink-2": "text-ink-2",
  "ink-3": "text-ink-3",
};

/** A merged row stays quiet: its icon keeps the tone, its word does not. */
const WORD_TEXT: Partial<Record<State, string>> = { merged: "text-ink" };

const ring = <circle cx="8" cy="8" r="6.5" />;
const clock = <path d="M8 4.8V8l2.3 1.6" />;
const pause = (
  <>
    {ring}
    <path d="M6.6 5.6v4.8M9.4 5.6v4.8" />
  </>
);

const ICONS: Record<State, ReactNode> = {
  failed: (
    <>
      {ring}
      <path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" />
    </>
  ),
  question: (
    <>
      {ring}
      <path d="M6.2 6.3a1.9 1.9 0 1 1 2.7 1.7c-.6.3-.9.7-.9 1.2v.3M8 11.4v.1" />
    </>
  ),
  paused: pause,
  needs_you: (
    <>
      {ring}
      <path d="M8 4.8v3.6M8 11.1v.1" />
    </>
  ),
  stale: (
    <>
      <path d="M8 2 14.5 13.5h-13z" />
      <path d="M8 6.3v3.2M8 11.6v.1" />
    </>
  ),
  running: (
    <>
      <circle cx="8" cy="8" r="6.5" className="state-pulse" />
      <path d="M8 5.4a2.6 2.6 0 1 0 0 5.2a2.6 2.6 0 1 0 0-5.2z" fill="currentColor" stroke="none" />
    </>
  ),
  ready_to_merge: (
    <>
      {ring}
      <path d="M5 8.3 7.1 10.4 11 6.2" />
    </>
  ),
  merged: (
    <>
      <circle cx="8" cy="8" r="6.5" fill="currentColor" />
      <path d="M5.2 8.3 7.2 10.3 10.9 6.3" strokeWidth={1.8} style={{ stroke: "var(--canvas)" }} />
    </>
  ),
  queued: (
    <>
      {ring}
      {clock}
    </>
  ),
  on_ci: (
    <>
      <rect x="1.5" y="1.5" width="13" height="13" pathLength={16} strokeWidth={1.4} className="state-march" />
      {clock}
    </>
  ),
  parked: (
    <>
      {ring}
      <path d="M5.3 8h.1M8 8h.1M10.7 8h.1" />
    </>
  ),
  held: pause,
  todo: ring,
  backlog: <circle cx="8" cy="8" r="6.5" strokeDasharray="2 2.4" />,
};

/** The word stays the accessible name when hidden, so colour never carries the state alone. */
export function StateDot({ state, hideWord = false, tone = STATES[state].tone }: { state: State; hideWord?: boolean; tone?: Tone }) {
  const { word } = STATES[state];
  return (
    <span role="img" aria-label={word} data-state={state} className={`inline-flex items-center gap-1.5 ${TONE_TEXT[tone]}`}>
      <svg
        viewBox="0 0 16 16"
        width={14}
        height={14}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        className="size-3.5 shrink-0"
      >
        {ICONS[state]}
      </svg>
      {!hideWord && <span className={`font-sans text-[13px] font-medium ${WORD_TEXT[state] ?? ""}`}>{word}</span>}
    </span>
  );
}
