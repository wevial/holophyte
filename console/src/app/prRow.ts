import { waitingOnCi } from "../lib/attention";
import type { PullRequestFacts } from "../lib/contract.gen";
import { formatAge } from "../lib/format";
import type { AttentionItem } from "../lib/types";
import type { State, Tone } from "./state";

interface PrWait {
  status: string;
  tone: Tone;
  line: string;
}

const NOT_READY: Record<string, string> = {
  review_bypassable: "Review required",
  checks_pending: "Checks running",
  checks_failing: "Checks failing",
  conflicting: "Merge conflict with main",
  mergeable_unknown: "GitHub is checking mergeability",
  threads_unresolved: "Review threads unresolved",
  head_moved: "Branch moved since approval",
  github_unreadable: "GitHub could not be read",
};

/** `reason` is the latest successful readiness answer's, when not ready. */
export function prWait(state: State, item: AttentionItem, ageMs: number | null, reason?: string): PrWait {
  const withAge = (qualifier: string | null) => [qualifier, ageMs == null ? null : formatAge(ageMs)].filter((part) => part != null).join(" · ");
  if (state === "running" || state === "stale") {
    return { status: "Factory fixing", tone: "wait", line: withAge(typeof item.round === "number" ? `round ${item.round}` : null) };
  }
  const draft = item.pr != null && typeof item.pr === "object" && (item.pr as Partial<PullRequestFacts>).state === "draft";
  if (draft && state !== "on_ci") return { status: withAge("Draft"), tone: "need", line: "" };
  const line = withAge(state === "on_ci" ? waitingOnCi(item) : state === "ready_to_merge" ? "ready to merge" : null);
  if (state === "on_ci") return { status: "On CI", tone: "ink-2", line };
  const notReady = state === "ready_to_merge" || reason == null ? undefined : NOT_READY[reason];
  return { status: notReady ?? "Your review", tone: "need", line };
}
