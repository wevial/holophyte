import { waitingOnCi, type Fact } from "../lib/attention";
import type { PullRequestFacts } from "../lib/contract.gen";
import { formatAge } from "../lib/format";
import { prLabel } from "../lib/shipped";
import type { AttentionItem } from "../lib/types";
import type { State, Tone } from "./state";

interface PrWait {
  status: string;
  tone: Tone;
  line: string;
}

export function prWait(state: State, item: AttentionItem, ageMs: number | null): PrWait {
  const withAge = (qualifier: string | null) => [qualifier, ageMs == null ? null : formatAge(ageMs)].filter((part) => part != null).join(" · ");
  if (state === "running" || state === "stale") {
    return { status: "Factory fixing", tone: "wait", line: withAge(typeof item.round === "number" ? `round ${item.round}` : null) };
  }
  const draft = item.pr != null && typeof item.pr === "object" && (item.pr as Partial<PullRequestFacts>).state === "draft";
  const line = withAge(state === "on_ci" ? waitingOnCi(item) : draft ? "draft" : state === "ready_to_merge" ? "ready to merge" : null);
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

function waitsOnFacts(waitsOn: unknown): Fact[] {
  if (!Array.isArray(waitsOn)) return [];
  return waitsOn.flatMap((entry: { ticket?: unknown; pr_url?: unknown }) => {
    const label = typeof entry?.pr_url === "string" ? prLabel(entry.pr_url) : typeof entry?.ticket === "string" ? entry.ticket : null;
    return label == null ? [] : [{ label: `waits on ${label}`, tone: "warn" as const }];
  });
}

export function rowFacts(
  pr: Pick<PullRequestFacts, "checks" | "review" | "open_threads"> | null,
  { round, waits_on }: { round?: unknown; waits_on?: unknown } = {},
): Fact[] {
  const facts: Fact[] = [
    (pr?.checks != null ? CHECKS[pr.checks] : undefined) ?? { label: "checks unknown", tone: "neutral" },
    threadsFact(pr?.open_threads),
  ];
  if (pr?.review === "changes_requested") facts.push({ label: "changes requested", tone: "bad" });
  if (typeof round === "number") facts.push({ label: `${round} ${round === 1 ? "round" : "rounds"}`, tone: "neutral" });
  return [...facts, ...waitsOnFacts(waits_on)];
}
