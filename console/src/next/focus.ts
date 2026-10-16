import type { PrFacts } from "../lib/attention";
import { prLabel } from "../lib/shipped";
import type { AttentionItem } from "../lib/types";

export function focusPrLabel(item: AttentionItem): string | null {
  const pr = item.pr as PrFacts | null | undefined;
  if (typeof pr?.number === "number" && Number.isFinite(pr.number)) return `PR #${pr.number}`;
  return typeof item.pr_url === "string" && item.pr_url ? prLabel(item.pr_url) : null;
}

export function blockedHeadline(item: AttentionItem): string {
  if (item.park_kind !== "fix_declined") return "The run has a question for you";
  const label = focusPrLabel(item);
  const onPr = label == null ? "" : ` on ${label}`;
  const pr = item.pr as PrFacts | null | undefined;
  return pr?.checks === "failure"
    ? `Checks failed${onPr} and the fix round found nothing to change`
    : `The fix round${onPr} found nothing to change`;
}

export function blockedAsk(item: AttentionItem): string {
  let ask = typeof item.question === "string" ? item.question : "";
  const lines = ask.split("\n");
  if (item.pr_url && lines[0]?.trim() === `PR open: ${item.pr_url}`) ask = lines.slice(1).join("\n");
  const marker = "operator instruction needed:";
  const start = ask.indexOf(marker);
  return (start < 0 ? ask : ask.slice(start + marker.length)).trim();
}
