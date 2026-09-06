import type { Finding, Round } from "./types";

export type Severity = "must" | "should" | "nit";

const RANK: Record<Severity, number> = { must: 0, should: 1, nit: 2 };

/** Anything the reviewer did not call `must` or `should` sits on the nit pill. */
export function severityOf(finding: Finding): Severity {
  const severity = finding.severity.toLowerCase();
  return severity === "must" || severity === "should" ? severity : "nit";
}

/** The newest round's findings, most severe first; none once the newest
 *  round passed, and none before any round is recorded. */
export function openFindings(rounds: Round[]): Finding[] {
  if (rounds.length === 0) return [];
  const newest = rounds.reduce((best, round) => (round.round > best.round ? round : best));
  if (newest.verdict === "pass") return [];
  return [...newest.findings].sort((a, b) => RANK[severityOf(a)] - RANK[severityOf(b)]);
}

export function severityCounts(findings: Finding[]): Record<Severity, number> {
  const counts: Record<Severity, number> = { must: 0, should: 0, nit: 0 };
  for (const finding of findings) counts[severityOf(finding)] += 1;
  return counts;
}
