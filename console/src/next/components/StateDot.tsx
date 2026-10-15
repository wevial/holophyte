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

const pause = <path d="M6 4.5v7M10 4.5v7" />;

const ICONS: Record<State, ReactNode> = {
  failed: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" />
    </>
  ),
  question: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M6.3 6.4a1.8 1.8 0 1 1 2.5 1.6c-.5.2-.8.6-.8 1.1v.3M8 11.3v.1" />
    </>
  ),
  paused: pause,
  needs_you: (
    <>
      <circle cx="8" cy="5.5" r="2.5" />
      <path d="M3.5 13.5c.6-2.5 2.4-4 4.5-4s3.9 1.5 4.5 4" />
    </>
  ),
  stale: <path d="M4.5 2.5h7M4.5 13.5h7M5.5 2.5c0 3 5 3.5 5 5.5s-5 2.5-5 5.5M10.5 2.5c0 3-5 3.5-5 5.5s5 2.5 5 5.5" />,
  running: (
    <>
      <circle cx="8" cy="8" r="6" className="state-pulse" />
      <circle cx="8" cy="8" r="2" fill="currentColor" />
    </>
  ),
  ready_to_merge: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M5.3 8.2l1.9 1.9 3.5-3.8" />
    </>
  ),
  merged: (
    <>
      <circle cx="5" cy="3.5" r="1.5" />
      <circle cx="5" cy="12.5" r="1.5" />
      <circle cx="11.5" cy="9" r="1.5" />
      <path d="M5 5v6M5 6c0 2.2 2.2 3 5 3" />
    </>
  ),
  queued: <path d="M3 4.5h10M3 8h10M3 11.5h6" />,
  on_ci: <rect x="2.5" y="2.5" width="11" height="11" strokeDasharray="2.5 2" className="state-march" />,
  parked: (
    <>
      <rect x="2.5" y="2.5" width="11" height="11" />
      <path d="M6.5 11V5h2.2a1.8 1.8 0 0 1 0 3.6H6.5" />
    </>
  ),
  held: pause,
  todo: <circle cx="8" cy="8" r="5.5" />,
  backlog: <circle cx="8" cy="8" r="5.5" strokeDasharray="1.5 2.5" />,
};

/** The word stays the accessible name when hidden, so colour never carries the state alone. */
export function StateDot({ state, hideWord = false }: { state: State; hideWord?: boolean }) {
  const { word, tone } = STATES[state];
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
      {!hideWord && <span className="font-sans text-[13px] font-medium">{word}</span>}
    </span>
  );
}
