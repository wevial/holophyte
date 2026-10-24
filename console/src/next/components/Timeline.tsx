import type { ReactNode } from "react";
import { formatClock, formatSpan } from "../../lib/format";
import type { ActivityStep, ActivityTone } from "../activity";
import { FoldChevron } from "./icons";
import { StateDot } from "./StateDot";

export const ROW = "grid grid-cols-[48px_30px_minmax(0,1fr)_auto_14px] items-center gap-x-3.5 max-[720px]:grid-cols-[42px_30px_minmax(0,1fr)_auto_14px] max-[720px]:gap-x-2.5";
const BADGE_CENTRE = 27;
const CONNECTOR = { first: { top: BADGE_CENTRE, bottom: 0 }, middle: { top: 0, bottom: 0 }, last: { top: 0, height: BADGE_CENTRE } };

export type Line = keyof typeof CONNECTOR | "none";

/** Where row `index` of `count` sits on the connector. */
export function lineOf(index: number, count: number): Line {
  return count < 2 ? "none" : index === 0 ? "first" : index === count - 1 ? "last" : "middle";
}

/** The 1px line through the badges, from this row's badge to its neighbours'. */
export function Connector({ line, shifted = false }: { line: Line; shifted?: boolean }) {
  if (line === "none") return null;
  return <span aria-hidden="true" className={`absolute left-[76px] w-px bg-rule max-[720px]:left-[66px] ${shifted ? "ml-3" : ""}`} style={CONNECTOR[line]} />;
}

const BADGE_TONE: Record<ActivityTone, string> = {
  ok: "bg-live-wash text-live",
  warn: "bg-warn-wash text-warn-ink",
  bad: "bg-bad-wash text-bad",
  wait: "bg-wait-wash text-wait",
  act: "bg-act-wash text-act",
  live: "bg-live-wash text-live",
  neutral: "bg-paper-2 text-ink-3",
};

const ICON_PATHS: Partial<Record<ActivityTone | "pickup", string>> = {
  ok: "M3.5 8.4 6.6 11.5 12.5 5",
  warn: "M3.2 8a4.8 4.8 0 1 0 1.4-3.4M3.2 2.6v2.6h2.6",
  bad: "M4.5 4.5l7 7M11.5 4.5l-7 7",
  wait: "M8 1.5a6.5 6.5 0 1 1 0 13a6.5 6.5 0 1 1 0-13zM8 4.8V8l2.3 1.6",
  act: "M10.6 2.6l2.8 2.8-7.6 7.6H3v-2.8zM9 4.2l2.8 2.8",
  pickup: "M4 2.5v6h8.5M9.5 5.5l3 3-3 3",
  neutral: "M8 5.5a2.5 2.5 0 1 1 0 5a2.5 2.5 0 1 1 0-5z",
};

/** A group's tone badge: the live dot while it runs, else its tone's icon, or `icon`'s. */
export function Badge({ tone, running, icon }: { tone: ActivityTone; running: boolean; icon?: "pickup" }) {
  const path = ICON_PATHS[icon ?? tone];
  return (
    <span data-badge={tone} className={`relative z-[1] flex size-[30px] items-center justify-center rounded-md ${BADGE_TONE[tone]}`}>
      {running ? (
        <StateDot state="running" hideWord tone="live" />
      ) : (
        <svg viewBox="0 0 16 16" width={16} height={16} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d={path} />
        </svg>
      )}
    </span>
  );
}

/** A group's head row: its start time, badge, and `children` (its title and
 *  duration cells), as a fold button with a chevron when it `folds`. */
export function GroupHead({ at, badge, folds, open, controls, onToggle, children }: {
  at: number | null;
  badge: ReactNode;
  folds: boolean;
  open: boolean;
  controls: string;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className={`${ROW} min-h-[54px] py-3`}>
      {at == null ? <span /> : (
        <time dateTime={new Date(at).toISOString()} className="font-mono text-[13px] text-ink-2">
          {formatClock(at)}
        </time>
      )}
      {badge}
      {folds ? (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={controls}
          onClick={onToggle}
          className="group col-span-3 col-start-3 grid cursor-pointer grid-cols-subgrid items-center border-0 bg-transparent p-0 text-left"
        >
          {children}
          <FoldChevron open={open} />
        </button>
      ) : (
        <div className="col-span-3 col-start-3 grid grid-cols-subgrid items-center">
          {children}
        </div>
      )}
    </div>
  );
}

/** A timed step reads its label bold and its duration in mono, before the rest of its text. */
function StepText({ step }: { step: ActivityStep }) {
  if (step.ms == null) return <>{step.text}</>;
  const [label, ...rest] = step.text.split(" · ");
  return (
    <>
      <b className="font-semibold text-ink">{label}</b> <span className="font-mono text-[13px] text-ink-2">{step.ms < 1000 ? "<1s" : formatSpan(step.ms)}</span>
      {rest.length > 0 && ` · ${rest.join(" · ")}`}
    </>
  );
}

export function Steps({ steps }: { steps: ActivityStep[] }) {
  return (
    <ol className="m-0 list-none p-0 pb-3">
      {steps.map((step, index) => (
        <li key={`${step.at}:${index}`} data-step className={`${ROW} !items-baseline py-1`}>
          <time dateTime={new Date(step.at).toISOString()} className="font-mono text-[13px] text-ink-3">
            {formatClock(step.at)}
          </time>
          <span data-step-text className={`col-span-3 col-start-3 font-sans text-[14px] leading-[1.45] [overflow-wrap:anywhere] ${step.quiet ? "text-ink-2" : "text-ink"}`}>
            <StepText step={step} />
          </span>
        </li>
      ))}
    </ol>
  );
}
