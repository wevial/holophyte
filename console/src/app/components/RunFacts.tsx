import type { ReactNode } from "react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@ui-sdk/react/tooltip";
import type { Fact } from "../../lib/attention";
import type { RunDetail } from "../../lib/contract.gen";
import { formatClock, formatSettled } from "../../lib/format";
import { prLabel, shortSha } from "../../lib/shipped";
import { zoneName } from "../pages/runShared";

type Run = RunDetail["run"];

const TIP = "z-50 rounded-[6px] border border-rule-2 bg-raised px-2.5 py-1 text-[12px] font-medium whitespace-nowrap text-ink shadow-card";
const FACT = "inline-flex items-center gap-1.5 rounded-[4px] font-mono text-[13px] text-ink-2 outline-offset-2";

const ICONS = {
  Implementer: (
    <>
      <circle cx="8" cy="5.5" r="2.5" />
      <path d="M3.5 13.5a4.5 4.5 0 0 1 9 0" />
    </>
  ),
  Reviewer: (
    <>
      <path d="M1.5 8s2.4-4.5 6.5-4.5S14.5 8 14.5 8 12.1 12.5 8 12.5 1.5 8 1.5 8z" />
      <circle cx="8" cy="8" r="1.8" />
    </>
  ),
  "Pull request": (
    <>
      <circle cx="5" cy="4" r="1.5" />
      <circle cx="5" cy="12" r="1.5" />
      <circle cx="11" cy="12" r="1.5" />
      <path d="M5 5.5v5M11 10.5V7a2 2 0 0 0-2-2H7.5" />
    </>
  ),
  Started: (
    <>
      <circle cx="8" cy="8" r="6" />
      <path d="M8 5v3l2 1.3" />
    </>
  ),
  Host: (
    <>
      <rect x="2.5" y="3" width="11" height="4" rx="1" />
      <rect x="2.5" y="9" width="11" height="4" rx="1" />
      <path d="M5 5h.1M5 11h.1" />
    </>
  ),
};

/** A trigger whose tooltip opens alike on hover and keyboard focus. */
function Tip({ tip, href, children, className }: { tip: string; href?: string; children: ReactNode; className: string }) {
  return (
    <Tooltip delay={150}>
      <TooltipTrigger
        render={({ type: _button, ...trigger }) =>
          href == null ? (
            <span {...trigger} tabIndex={0} className={className}>{children}</span>
          ) : (
            <a {...trigger} href={href} target="_blank" rel="noreferrer" className={`${className} hover:text-ink`}>{children}</a>
          )
        }
      />
      <TooltipContent side="bottom" sideOffset={6} className={TIP}>{tip}</TooltipContent>
    </Tooltip>
  );
}

function Fact({ name, tip = name, href, tone = "", children }: { name: keyof typeof ICONS; tip?: string; href?: string; tone?: string; children: ReactNode }) {
  return (
    <Tip tip={tip} href={href} className={FACT}>
      <svg
        viewBox="0 0 16 16"
        width={16}
        height={16}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.4}
        strokeLinecap="round"
        strokeLinejoin="round"
        role="img"
        aria-label={name}
        className={`size-4 shrink-0 ${tone}`}
      >
        {ICONS[name]}
      </svg>
      {children}
    </Tip>
  );
}

/** What holds the pull request: a merge's sha and time, else a park's
 *  facts with "ready for your merge" once its readiness says so. */
function pullRequestHolds(facts: Fact[] | null, ready: boolean, merge: Pick<Run, "merge_sha" | "ended_ms"> | null): string {
  if (merge != null) {
    const sha = shortSha(merge.merge_sha);
    if (!sha) return "";
    return `merged as ${sha}${merge.ended_ms == null ? "" : ` at ${formatClock(merge.ended_ms)}`}`;
  }
  return [...(facts ?? []).map((fact) => fact.label), ...(ready ? ["ready for your merge"] : [])].join(" · ");
}

/** The run's facts under its stepper, each an icon named for the fact with
 *  its value beside it and the fact's name on hover or focus. */
export function RunFacts({ run, labels, prFacts, ready, merged }: {
  run: Run;
  labels: { implementer: string; reviewer: string } | null;
  prFacts: Fact[] | null;
  ready: boolean;
  merged: boolean;
}) {
  return (
    <div data-run-facts-row className="mt-5 flex flex-wrap gap-x-6 gap-y-2">
      {labels != null && (
        <>
          <Fact name="Implementer">{labels.implementer}</Fact>
          <Fact name="Reviewer">{labels.reviewer}</Fact>
        </>
      )}
      {run.pr_url != null && (
        <Fact name="Pull request" tip={pullRequestHolds(prFacts, ready, merged ? run : null) || "Pull request"} href={run.pr_url} tone={ready ? "text-live" : ""}>
          {prLabel(run.pr_url)}
        </Fact>
      )}
      <Fact name="Started">
        {formatClock(run.started_ms)} {zoneName(run.started_ms)}
      </Fact>
      {run.host != null && <Fact name="Host">{run.host}</Fact>}
    </div>
  );
}

/** The agent time against its box: a 96 by 3 px bar filled to the time's
 *  share and both spans; the time alone when the run has no box. */
export function TimeBox({ agentMs, boxMs }: { agentMs: number; boxMs: number | null }) {
  const used = formatSettled(agentMs);
  if (boxMs == null || boxMs <= 0) return <span className="font-mono text-[13px] text-ink-3">{used}</span>;
  const box = formatSettled(boxMs);
  const share = Math.min(100, Math.round((agentMs / boxMs) * 100));
  return (
    <Tip tip={`${used} of a ${box} box`} className="inline-flex items-center gap-2.5 rounded-[4px] font-mono text-[13px] text-ink-3 outline-offset-2">
      <span aria-hidden="true" className="h-[3px] w-24 overflow-hidden rounded-[2px] bg-rule-2">
        <span data-time-fill className="block h-full bg-focus" style={{ width: `${share}%` }} />
      </span>
      {used} / {box}
    </Tip>
  );
}
