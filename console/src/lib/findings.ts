import type { Finding, Round } from "./types";

export type Severity = "must" | "should" | "nit";

const RANK: Record<Severity, number> = { must: 0, should: 1, nit: 2 };

/** The store holds the reviewer's p0, p1, p2 and nit; the pill collapses
 *  them: p0 and p1 are must, p2 is should, anything else is nit. */
export function severityOf(finding: Finding): Severity {
  const severity = finding.severity.toLowerCase();
  if (severity === "p0" || severity === "p1") return "must";
  if (severity === "p2") return "should";
  return "nit";
}

// The mounts a reviewer has cited the candidate under: the read-only
// `/workspace/` bind, and the writable copy at `/home/reviewer/candidate`
// the agent has run on since KO-366. Rounds recorded before the parser
// stripped the second still carry it on `path`.
const CONTAINER_PREFIXES = ["/workspace/", "/home/reviewer/candidate/"];

/** The finding's path made repository-relative. */
export function findingPath(finding: Finding): string {
  const prefix = CONTAINER_PREFIXES.find((prefix) => finding.path.startsWith(prefix));
  return prefix ? finding.path.slice(prefix.length) : finding.path;
}

/** A finding's message split for the card. */
export interface FindingParts {
  /** The bold lead, or "Criterion n · status"; null for a plain message. */
  title: string | null;
  /** What remains once the bullet, the severity marker, the lead and the
   *  location link are out; rendered as Markdown by the card. */
  body: string;
  /** A criterion finding's own text, folded under a disclosure. */
  criterion: string | null;
}

// The reviewer's checklist line, the shape `holophyte/review.py` writes and
// reads: `CRITERION n: met|not met|unwitnessed — reason`, with the
// criterion's own text on the following lines.
const CRITERION_RE = /^\s*CRITERION\s+(\d+)\s*:\s*(met|not met|unwitnessed)\b\s*(?:[-–—:]+\s*)?(.*)$/i;
const BULLET_RE = /^\s*(?:[-*+]|\d+[.)])\s+/;
const SEVERITY_MARK_RE =
  /^(?:[[(]\s*(?:p0|p1|p2|nit|blocker)\s*[\])]\s*[:\-–—]?|(?:p0|p1|p2|nit|blocker)\b\s*[:\-–—])\s*/i;
const BOLD_LEAD_RE = /^\*\*([^*\n]+)\*\*/;
const SEPARATOR_RE = /^\s*[-–—:,;]+\s*/;
// A markdown link and the horizontal space around it; the reviewer's
// location link targets a file, so it is dropped — the card already shows
// `path:line` — while a URL the body renderer links is kept.
const LINK_RE = /[^\S\n]*\[[^\]\n]*\]\(([^)\s]+)\)[^\S\n]*/g;
const URL_TARGET_RE = /^(?:https?:\/\/|mailto:)/i;

export function findingParts(message: string): FindingParts {
  const newline = message.indexOf("\n");
  const head = newline < 0 ? message : message.slice(0, newline);
  const criterion = CRITERION_RE.exec(head);
  if (criterion) {
    const tail = newline < 0 ? "" : message.slice(newline + 1).trim();
    return {
      title: `Criterion ${criterion[1]} · ${criterion[2]!.toLowerCase()}`,
      body: (criterion[3] ?? "").trim(),
      criterion: tail === "" ? null : tail,
    };
  }
  let text = message.trim().replace(BULLET_RE, "").replace(SEVERITY_MARK_RE, "");
  let title: string | null = null;
  const bold = BOLD_LEAD_RE.exec(text);
  if (bold) {
    title = bold[1]!.trim();
    text = text.slice(bold[0].length);
  }
  let body = text.replace(LINK_RE, (link, target) => (URL_TARGET_RE.test(String(target)) ? link : " "));
  if (bold) body = body.replace(SEPARATOR_RE, "");
  return { title, body: body.trim(), criterion: null };
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
