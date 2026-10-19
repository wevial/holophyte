import { waitingOnCi, type Fact } from "../lib/attention";
import type { PullRequestFacts } from "../lib/contract.gen";
import { formatAge } from "../lib/format";
import type { AttentionItem } from "../lib/types";
import type { State, Tone } from "./state";

interface PrWait {
  status: string;
  tone: Tone;
  line: string;
}

export function prWait(state: State, item: AttentionItem, ageMs: number | null): PrWait {
  const draft = item.pr != null && typeof item.pr === "object" && (item.pr as Partial<PullRequestFacts>).state === "draft";
  const qualifier = state === "on_ci" ? waitingOnCi(item) : draft ? "draft" : state === "ready_to_merge" ? "ready to merge" : null;
  const line = [qualifier, ageMs == null ? null : formatAge(ageMs)].filter((part) => part != null).join(" · ");
  return state === "on_ci" ? { status: "On CI", tone: "ink-2", line } : { status: "Your review", tone: "need", line };
}

const CHECKS: Record<string, Fact> = {
  success: { label: "checks green", tone: "ok" },
  failure: { label: "checks failing", tone: "bad" },
  pending: { label: "checks running", tone: "neutral" },
};

function threadsFact(open: unknown): Fact {
  if (open === 0) return { label: "0 threads", tone: "neutral" };
  if (typeof open === "number") return { label: `${open} open ${open === 1 ? "thread" : "threads"}`, tone: "warn" };
  if (typeof open === "string" && open !== "") return { label: `${open} open threads`, tone: "warn" };
  return { label: "threads unknown", tone: "neutral" };
}

export function rowFacts(pr: Pick<PullRequestFacts, "checks" | "review" | "open_threads"> | null): Fact[] {
  const facts = [
    (pr?.checks != null ? CHECKS[pr.checks] : undefined) ?? { label: "checks unknown", tone: "neutral" as const },
    threadsFact(pr?.open_threads),
  ];
  return pr?.review === "changes_requested" ? [...facts, { label: "changes requested", tone: "bad" }] : facts;
}
