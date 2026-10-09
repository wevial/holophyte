import { useState } from "react";
import { useMergeReadiness } from "../hooks/useMergeReadiness";
import { ROUTES, postAction } from "../lib/actions";
import type { Fetch } from "../lib/poll";
import { prLabel } from "../lib/shipped";
import { ActionButton } from "./ActionButton";

/** The line a not-ready pull request shows in place of Merge, by the
 *  readiness `reason` a person can wait on. */
const WAITING_LINES: Record<string, string> = {
  review_not_approved: "waiting on review approval",
  checks_pending: "checks running",
  checks_failing: "checks failing",
  conflicting: "merge conflict with main",
  mergeable_unknown: "GitHub is still checking mergeability",
  threads_unresolved: "review threads unresolved",
  head_moved: "branch moved since approval",
  github_unreadable: "GitHub could not be read",
};

/** Reasons that mean the run is not a human-approval park at all. */
const SILENT = new Set(["not_parked", "not_human_approval"]);

/** The one not-ready reason that draws a button instead of a line: the
 *  review is the only gap and the daemon's GitHub user may bypass it. */
const BYPASSABLE = "review_bypassable";

const pageFetch: Fetch = (url, init) => globalThis.fetch(url, init);

/** A parked pull request's Merge, drawn from the daemon's readiness for
 *  `runId`: a Merge button that opens an in-row confirm when ready, a
 *  "Merge (bypass review)" button when only a bypassable review is
 *  missing, else one line saying why, else nothing. Confirming posts
 *  `/actions/merge` once and shows the daemon's answer. */
export function MergeAction({ base, runId, prUrl, polls, deps, fetch }: {
  base: string;
  runId: number;
  prUrl?: string | null;
  polls: number;
  deps: { fetch: Fetch };
  fetch?: Fetch;
}) {
  const readiness = useMergeReadiness(base, runId, polls, deps);
  const [confirming, setConfirming] = useState<"merge" | "bypass" | null>(null);
  const [merged, setMerged] = useState(false);
  const [posting, setPosting] = useState(false);
  const [answer, setAnswer] = useState<{ text: string; ok: boolean } | null>(null);
  const merge = async () => {
    setPosting(true);
    try {
      const body = confirming === "bypass" ? { run: runId, bypass_review: true } : { run: runId };
      const result = await postAction(base, ROUTES.Merge!, body, fetch ?? pageFetch);
      setAnswer({ text: result.detail, ok: result.ok });
      if (result.ok) setMerged(true);
    } finally {
      setPosting(false);
      setConfirming(null);
    }
  };
  const reason = readiness?.reason ?? null;
  const bypassable = readiness?.ready === false && reason === BYPASSABLE;
  const open = (kind: "merge" | "bypass") => async () => { setAnswer(null); setConfirming(kind); };
  if (confirming && !posting && !(confirming === "bypass" ? bypassable : readiness?.ready === true)) setConfirming(null);
  const waiting = readiness && !readiness.ready && reason != null && !SILENT.has(reason) && !bypassable
    ? WAITING_LINES[reason] ?? readiness.detail : null;
  const bypassed = readiness?.facts.find(fact => fact.name === "review_approved")?.detail;
  const pr = prUrl ? prLabel(prUrl) : "the pull request";
  return (
    <div className="flex flex-col items-end gap-1.5">
      {!merged && (confirming ? (
        <div role="group" aria-label={confirming === "bypass" ? "Confirm bypass merge" : "Confirm merge"}
          className="flex flex-col items-end gap-1.5">
          <p className="text-[12px] text-ink">
            {confirming === "bypass" ? `Bypass the required review and merge ${pr} into main?` : `Merge ${pr} into main?`}
          </p>
          {confirming === "bypass" && bypassed && <p className="max-w-[280px] text-right text-[12px] text-muted">{bypassed}</p>}
          <div className="flex gap-1.5">
            <ActionButton onAct={merge}>{confirming === "bypass" ? "Confirm bypass merge" : "Confirm merge"}</ActionButton>
            <button type="button" disabled={posting} onClick={() => setConfirming(null)}
              className="rounded-button px-2 py-1 text-[12px] text-muted disabled:opacity-60">Cancel</button>
          </div>
        </div>
      ) : readiness?.ready === true ? <ActionButton onAct={open("merge")}>Merge</ActionButton>
        : bypassable && <ActionButton onAct={open("bypass")}>Merge (bypass review)</ActionButton>)}
      {!merged && waiting && <p data-merge-waiting className="text-right text-[12px] text-muted">{waiting}</p>}
      {answer && (
        <p data-action-detail data-ok={answer.ok} role="status"
          className={`max-w-[280px] text-right text-[12px] ${answer.ok ? "text-muted" : "text-needs-you-link"}`}>
          {answer.text}
        </p>
      )}
    </div>
  );
}
