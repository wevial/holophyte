import { useContext, useId, useState, type ReactNode } from "react";
import { Link } from "@tanstack/react-router";
import type { RoundCriterion, RunDetail } from "../../lib/contract.gen";
import { formatClock } from "../../lib/format";
import { shortSha } from "../../lib/shipped";
import type { RunActivity } from "../activity";
import { failureWord, firstLine } from "../history";
import { useTicket } from "../queries";
import { runPage } from "../run";
import { PollDepsContext } from "../scope";
import { FoldChevron } from "./icons";

type Tone = "bad" | "wait" | "ok" | "neutral";

const CARD_TONE: Record<Tone, string> = {
  bad: "bg-bad-wash shadow-[inset_4px_0_0_var(--bad)]",
  wait: "bg-wait-wash shadow-[inset_4px_0_0_var(--wait)]",
  ok: "bg-live-wash shadow-[inset_4px_0_0_var(--live)]",
  neutral: "bg-paper-2 shadow-[inset_4px_0_0_var(--rule)]",
};

const ICON: Record<Tone, { path: string; className: string }> = {
  bad: { path: "M4.5 4.5l7 7M11.5 4.5l-7 7", className: "text-bad" },
  wait: { path: "M3 11V9a4 4 0 0 1 4-4h6.5M10.5 2l3 3-3 3", className: "text-wait" },
  ok: { path: "M3.5 8.4 6.6 11.5 12.5 5", className: "text-live" },
  neutral: { path: "M8 5.5a2.5 2.5 0 1 1 0 5a2.5 2.5 0 1 1 0-5z", className: "text-ink-3" },
};

const HANDED_TO_BABYSITTER = /^sent back to the babysitter/;
const PASSED = new Set(["pass", "approved"]);

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

function Card({ tone, title, aside, children }: { tone: Tone; title: ReactNode; aside?: ReactNode; children?: ReactNode }) {
  const icon = ICON[tone];
  return (
    <section aria-label="How the run ended" data-end-card={tone} className={`mt-4 border-b-2 border-ink py-3.5 pl-5 pr-5 max-[720px]:pl-4 ${CARD_TONE[tone]}`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <svg viewBox="0 0 16 16" width={18} height={18} fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={`self-center ${icon.className}`}>
          <path d={icon.path} />
        </svg>
        <b data-end-title className="font-sans text-[16px] font-semibold leading-[1.3] text-ink">{title}</b>
        {aside != null && <span className="ml-auto font-mono text-[13px] text-ink-2">{aside}</span>}
      </div>
      {children != null && <div className="mt-1.5 pl-[30px] font-sans text-[14px] leading-[1.5] text-ink">{children}</div>}
    </section>
  );
}

/** Where the Now bar stood once a run has ended: how it ended, and the ticket's next run when there is one. */
export function EndCard({ detail, activity, base, project }: { detail: RunDetail; activity: RunActivity; base: string; project: string }) {
  const next = useNextRun(base, detail);
  const { run } = detail;
  const nextLink = next == null ? null : <Link {...runPage(project, next)} className="font-mono underline underline-offset-2 hover:text-ink">run {next}</Link>;
  const reason = run.outcome_reason ?? "";
  const final = activity.groups.findLast((group) => group.kind === "final")?.round;
  if (run.outcome === "failed" && final != null && final.ended_ms != null && !PASSED.has(final.verdict)) {
    const review = activity.groups.findLast((group) => group.kind === "round" && (group.round?.criteria.length ?? 0) > 0)?.round ?? null;
    const parked = detail.events.some((event) => event.kind === "steer_cap_park");
    return (
      <Card tone="bad" title="Failed at the final check" aside={run.ended_ms == null ? null : formatClock(run.ended_ms)}>
        <ul className="m-0 list-disc pl-5 [&>li+li]:mt-1">
          <li>{finalCheckBullet(final.criteria, review?.criteria ?? null, review?.round ?? null)}</li>
          {parked && <li>Its review rounds were used up, so it parked on the steer cap.</li>}
          {nextLink != null && (
            <li>
              <b className="font-semibold">In motion:</b> the ticket's next attempt → {nextLink}
            </li>
          )}
        </ul>
        {reason !== "" && <RawReason reason={reason} />}
      </Card>
    );
  }
  if (run.outcome === "failed") return <Card tone="bad" title={capital(failureWord(run.failure_kind))}>{firstLine(reason) || null}</Card>;
  if (run.outcome === "abandoned" && HANDED_TO_BABYSITTER.test(reason)) {
    const aside = (
      <>
        {run.ended_ms != null && formatClock(run.ended_ms)}
        {nextLink != null && <> · {nextLink} next</>}
      </>
    );
    return <Card tone="wait" title="Handed to the babysitter" aside={aside}>{capital(firstLine(reason))}</Card>;
  }
  if (run.outcome === "merged") {
    const sha = shortSha(run.merge_sha);
    const shaText = run.commit_url != null && sha !== "" ? <a href={run.commit_url} target="_blank" rel="noreferrer" className="font-mono underline underline-offset-2">{sha}</a> : <span className="font-mono">{sha}</span>;
    const at = run.ended_ms == null ? null : ` at ${formatClock(run.ended_ms)}`;
    return <Card tone="ok" title={sha === "" ? <>Merged{at}</> : <>Merged as {shaText}{at}</>} />;
  }
  return <Card tone="neutral" title={capital(run.outcome ?? "ended")}>{firstLine(reason) || null}</Card>;
}
