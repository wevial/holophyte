import { useContext, useId, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import type { Round, RoundCriterion, RunDetail } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { shortSha } from "../../lib/shipped";
import { PASSED, type RunActivity } from "../activity";
import { failureWord, firstLine } from "../history";
import { useTicket } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { FoldChevron } from "./icons";

export type EndTone = "bad" | "wait" | "ok" | "neutral";

/** The current group's wash and 4px left stripe: a live run's, or the tone its end took. */
export const CURRENT_TINT: Record<EndTone | "live", string> = {
  live: "bg-live-wash shadow-[inset_4px_0_0_var(--live)]",
  bad: "bg-bad-wash shadow-[inset_4px_0_0_var(--bad)]",
  wait: "bg-wait-wash shadow-[inset_4px_0_0_var(--wait)]",
  ok: "bg-live-wash shadow-[inset_4px_0_0_var(--live)]",
  neutral: "bg-paper-2 shadow-[inset_4px_0_0_var(--rule)]",
};

export type Ending =
  | { kind: "final_check"; tone: "bad"; final: Round; review: Round | null; parked: boolean }
  | { kind: "failed" | "other"; tone: "bad" | "neutral" }
  | { kind: "handed"; tone: "wait" }
  | { kind: "merged"; tone: "ok" };

const HANDED_TO_BABYSITTER = /^sent back to the babysitter/;

/** How an ended run ended, and the tone its last group wears for it. */
export function endingOf(detail: RunDetail, activity: RunActivity): Ending {
  const { run } = detail;
  const final = activity.groups.findLast((group) => group.kind === "final")?.round;
  if (run.outcome === "failed" && final?.ended_ms != null && !PASSED.has(final.verdict)) {
    const review = activity.groups.findLast((group) => group.kind === "round" && (group.round?.criteria.length ?? 0) > 0)?.round ?? null;
    return { kind: "final_check", tone: "bad", final, review, parked: detail.events.some((event) => event.kind === "steer_cap_park") };
  }
  if (run.outcome === "failed") return { kind: "failed", tone: "bad" };
  if (run.outcome === "abandoned" && HANDED_TO_BABYSITTER.test(run.outcome_reason ?? "")) return { kind: "handed", tone: "wait" };
  if (run.outcome === "merged") return { kind: "merged", tone: "ok" };
  return { kind: "other", tone: "neutral" };
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
const count = (criteria: RoundCriterion[], status: RoundCriterion["status"]) => criteria.filter((criterion) => criterion.status === status).length;

/** The ticket's first run after this one, when its answer lists one. */
function useNextRun(base: string, detail: RunDetail): number | null {
  const deps = useContext(PollDepsContext);
  const { data } = useTicket(base, detail.run.ticket, deps);
  const later = (data?.runs ?? []).map((run) => run.run).filter((id) => id > detail.run.id);
  return later.length > 0 ? Math.min(...later) : null;
}

function finalCheckBullet(final: RoundCriterion[], review: RoundCriterion[] | null, round: number | null): string {
  const total = final.length;
  const left = [count(final, "unwitnessed") > 0 && `${count(final, "unwitnessed")} of ${total} unwitnessed`, count(final, "not_met") > 0 && `${count(final, "not_met")} of ${total} not met`];
  const words = left.filter((phrase) => phrase !== false);
  const head = words.length > 0 ? `The final check left ${words.join(" and ")}.` : "The final check asked for changes.";
  if (review == null || round == null) return head;
  const open = review.filter((criterion) => criterion.status !== "met").map((criterion) => criterion.id);
  const tail = open.length > 0 ? `; ${open.join(", ")} ${open.length === 1 ? "was" : "were"} open` : "";
  return `${head} Round ${round} had ${count(review, "met")} of ${review.length} met${tail}.`;
}

function RawReason({ reason }: { reason: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="mt-2">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen(!open)}
        className="group inline-flex cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 font-mono text-[13px] text-ink-2 hover:text-ink"
      >
        <FoldChevron open={open} />
        raw reason
      </button>
      <pre id={id} hidden={!open} data-raw-reason className="m-0 mt-1.5 whitespace-pre-wrap font-mono text-[12px] leading-[1.5] text-ink-2 [overflow-wrap:anywhere]">
        {open && reason}
      </pre>
    </div>
  );
}

function Facts({ title, aside, children }: { title: ReactNode; aside?: ReactNode; children?: ReactNode }) {
  return (
    <div data-end-facts className="pb-3 pl-[106px] pr-5 font-sans text-[14px] leading-[1.5] text-ink max-[720px]:pl-[92px]">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <b data-end-title className="font-semibold">{title}</b>
        {aside != null && <span className="font-mono text-[13px] text-ink-2">{aside}</span>}
      </div>
      {children != null && <div className="mt-1">{children}</div>}
    </div>
  );
}

/** The end of an ended run's last group: how it ended, and the ticket's next run when there is one. */
export function EndFacts({ detail, ending, base, project }: { detail: RunDetail; ending: Ending; base: string; project: string }) {
  const next = useNextRun(base, detail);
  const { run } = detail;
  const nextLink = next == null ? null : <Link {...runPage(project, next)} className="font-mono underline underline-offset-2 hover:text-ink">run {next}</Link>;
  const reason = run.outcome_reason ?? "";
  if (ending.kind === "final_check") {
    const { final, review } = ending;
    return (
      <Facts title="Failed at the final check" aside={run.ended_ms == null ? null : formatClock(run.ended_ms)}>
        <ul className="m-0 list-disc pl-5 [&>li+li]:mt-1">
          <li>{finalCheckBullet(final.criteria, review?.criteria ?? null, review?.round ?? null)}</li>
          {ending.parked && <li>Its review rounds were used up, so it parked on the steer cap.</li>}
          {nextLink != null && (
            <li>
              <b className="font-semibold">In motion:</b> the ticket's next attempt → {nextLink}
            </li>
          )}
        </ul>
        {reason !== "" && <RawReason reason={reason} />}
      </Facts>
    );
  }
  if (ending.kind === "failed") return <Facts title={capital(failureWord(run.failure_kind))}>{firstLine(reason) || null}</Facts>;
  if (ending.kind === "handed") {
    const aside = (
      <>
        {run.ended_ms != null && formatClock(run.ended_ms)}
        {nextLink != null && <> · {nextLink} next</>}
      </>
    );
    return <Facts title="Handed to the babysitter" aside={aside}>{capital(firstLine(reason))}</Facts>;
  }
  if (ending.kind === "merged") {
    const sha = shortSha(run.merge_sha);
    const shaText = run.commit_url != null && sha !== "" ? <a href={run.commit_url} target="_blank" rel="noreferrer" className="font-mono underline underline-offset-2">{sha}</a> : <span className="font-mono">{sha}</span>;
    const at = run.ended_ms == null ? null : ` at ${formatClock(run.ended_ms)}`;
    return <Facts title={sha === "" ? <>Merged{at}</> : <>Merged as {shaText}{at}</>} />;
  }
  return <Facts title={capital(run.outcome ?? "ended")}>{firstLine(reason) || null}</Facts>;
}
