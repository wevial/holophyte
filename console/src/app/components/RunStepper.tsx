import type { MouseEvent, ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import type { MarkGlyph, StageState, StepperMark, StepperStage } from "../run";
import { STATE_ICONS } from "./StateDot";

const ring = <circle cx="8" cy="8" r="6.5" />;

const STAGE_GLYPH: Record<StageState, { icon: ReactNode; tone: string }> = {
  finished: { icon: STATE_ICONS.ready_to_merge, tone: "text-live" },
  current: { icon: STATE_ICONS.running, tone: "text-live" },
  failed: { icon: STATE_ICONS.failed, tone: "text-bad" },
  "not reached": { icon: STATE_ICONS.backlog, tone: "text-ink-3" },
};

const MARK_GLYPH: Record<MarkGlyph, { icon: ReactNode; tone: string }> = {
  loop: {
    icon: (
      <>
        {ring}
        <path d="M10.7 9.2a2.9 2.9 0 1 1-.4-2.9" />
        <path d="M10.6 4.6v1.9H8.7" />
      </>
    ),
    tone: "text-ask-ink",
  },
  pass: { icon: STATE_ICONS.ready_to_merge, tone: "text-live" },
  running: { icon: STATE_ICONS.running, tone: "text-live" },
  failed: { icon: STATE_ICONS.failed, tone: "text-bad" },
  ended: { icon: ring, tone: "text-ink-2" },
};

function Glyph({ icon, tone, size = "size-3.5" }: { icon: ReactNode; tone: string; size?: string }) {
  return (
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
      className={`${size} shrink-0 ${tone}`}
    >
      {icon}
    </svg>
  );
}

/** The console routes on the hash, so the mark scrolls to its round instead of navigating. */
function toRound(event: MouseEvent<HTMLAnchorElement>, anchor: string) {
  event.preventDefault();
  (document.getElementById(anchor) ?? document.getElementById("progress"))?.scrollIntoView({ block: "start" });
}

const MARK = "flex size-[22px] items-center justify-center rounded-[6px] outline-offset-1 hover:bg-hover data-[mark=running]:bg-live-wash data-[mark=running]:ring-1 data-[mark=running]:ring-live/40";

function Mark({ mark }: { mark: StepperMark }) {
  const glyph = <Glyph {...MARK_GLYPH[mark.glyph]} />;
  const { anchor } = mark;
  return (
    <Tooltip delay={150}>
      <TooltipTrigger
        data-mark={mark.glyph}
        aria-label={mark.label}
        className={MARK}
        render={
          anchor == null
            ? undefined
            : ({ type: _button, ...trigger }) => (
                <a {...trigger} href={`#${anchor}`} onClick={(event) => toRound(event, anchor)}>
                  {glyph}
                </a>
              )
        }
      >
        {glyph}
      </TooltipTrigger>
      <TooltipContent
        side="bottom"
        sideOffset={8}
        className="z-50 flex max-w-[320px] flex-col gap-0.5 rounded-[6px] border border-rule-2 bg-raised px-3 py-2 text-[12px] leading-[1.45] text-ink-2 shadow-card"
      >
        <span className="font-medium text-ink">{mark.card.title}</span>
        {mark.card.lines.map((line, index) => <span key={index}>{line}</span>)}
        {mark.card.model != null && <span className="font-mono text-ink-3">{mark.card.model}</span>}
      </TooltipContent>
    </Tooltip>
  );
}

function Marks({ stage }: { stage: StepperStage }) {
  if (stage.marks.length === 0 && stage.slots === 0) return null;
  return (
    <div role="group" aria-label={stage.name === "Review" ? "Review rounds" : "Check fix rounds"} className="mt-1.5 flex items-center gap-1">
      {stage.marks.map((mark) => (
        <span key={mark.label} className="contents">
          <Mark mark={mark} />
          {mark.subs.map((sub, index) => (
            <span key={index} role="img" aria-label={sub} data-sub-mark className="size-1 shrink-0 rounded-full bg-ink-3" />
          ))}
        </span>
      ))}
      {Array.from({ length: stage.slots }, (_, index) => (
        <span key={index} role="img" aria-label="unused round" data-slot className="flex size-[18px] items-center justify-center opacity-40">
          <Glyph icon={STATE_ICONS.backlog} tone="text-ink-3" size="size-2.5" />
        </span>
      ))}
    </div>
  );
}

/** The run's five stages left to right, joined by hairlines solid into a
 *  finished stage and dashed elsewhere, each stage's marks under it. */
export function RunStepper({ stages }: { stages: StepperStage[] }) {
  return (
    <ol aria-label="Stages" data-run-stepper className="m-0 mt-[18px] flex list-none items-start p-0">
      {stages.map((stage, index) => (
        <li key={stage.name} aria-label={`${stage.label}: ${stage.state}`} data-stage={stage.state} className={`flex min-w-0 items-start ${index === 0 ? "flex-none" : "flex-1"}`}>
          {index > 0 && (
            <span
              data-link={stage.state === "finished" ? "solid" : "dashed"}
              className={`mx-3 mt-[9px] min-w-4 flex-1 border-t border-rule-2 ${stage.state === "finished" ? "border-solid" : "border-dashed"}`}
            />
          )}
          <div className="flex-none">
            <div className={`flex items-center gap-1.5 whitespace-nowrap font-sans text-[13px] leading-[18px] ${stage.state === "not reached" ? "text-ink-3" : stage.state === "current" || stage.state === "failed" ? "font-medium text-ink" : "text-ink-2"}`}>
              <Glyph {...STAGE_GLYPH[stage.state]} />
              {stage.label}
            </div>
            <Marks stage={stage} />
          </div>
        </li>
      ))}
    </ol>
  );
}
