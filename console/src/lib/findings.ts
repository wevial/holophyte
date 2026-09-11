import type { LedgerRow } from "./ledger";
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

function relativePath(path: string): string {
  const prefix = CONTAINER_PREFIXES.find((prefix) => path.startsWith(prefix));
  return prefix ? path.slice(prefix.length) : path;
}

/** The finding's path made repository-relative. */
export function findingPath(finding: Finding): string {
  return relativePath(finding.path);
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

/** What became of a finding after its round: `fixed` when the next round
 *  no longer raises it, `declined` or `follow_up` when the implementer's
 *  ledger line adjudicated it that way, `open` when nothing after the
 *  round closed it — including every finding still standing in the run's
 *  last round. */
export type Fate = "fixed" | "declined" | "follow_up" | "open";

/** The chip's label for each fate. */
export const FATE_LABEL: Record<Fate, string> = {
  fixed: "fixed",
  declined: "declined",
  follow_up: "follow-up",
  open: "open",
};

export interface HistoryFinding {
  finding: Finding;
  fate: Fate;
  /** The implementer's DECLINE/FOLLOW_UP line when the ledger names one. */
  sentence: string | null;
}

/** One review round's findings with their fates. */
export interface RoundHistory {
  round: number;
  findings: HistoryFinding[];
}

// The key the store compares rounds by (store's `_finding_keys()`, §2's
// `path:line:severity`): a missing line keys at -1, and the fields join on
// the canonical form's unit separator so a path holding ":" cannot forge
// another finding's key.
function findingKey(finding: Finding): string {
  return [finding.path, finding.line ?? -1, finding.severity].join("\x1f");
}

// The ledger row a changes-requested round leaves (holophyte/loop.py):
// "Round N: REQUEST_CHANGES -> fix round", the reviewer's verdict, then
// "Implementer response:" and the fix round's reply, whose ADDRESS /
// FOLLOW_UP / DECLINE lines are the implementer's adjudications.
const RESPONSE_MARK = "Implementer response:";
// An optional Markdown bullet — `-`, `*` or `1.` — then its space, then
// the verdict word.
const ADJUDICATION_RE = /^\s*(?:(?:[-*+]|\d+[.)])\s+)?(DECLINE|FOLLOW[ _-]?UP)\b/i;
// A `path:line` citation in an adjudication line, e.g. `notes.md:10`.
const CITATION_RE = /[\w./-]+:\d+/g;
// A path-shaped token: `[\w./-]+` keeps the directory separators, so a
// token matches a path only whole — the `tests/config.py` token never
// names a `config.py` finding.
const PATH_TOKEN_RE = /[\w./-]+/g;

/** The implementer-response lines of `round`'s ledger row: the last
 *  `Round N:` row carrying the mark, else none. */
function responseLines(ledger: LedgerRow[], round: number): string[] {
  const head = `Round ${round}:`;
  for (let index = ledger.length - 1; index >= 0; index -= 1) {
    const row = ledger[index]!;
    if (row.kind !== "round" || !row.text.startsWith(head)) continue;
    const mark = row.text.indexOf(RESPONSE_MARK);
    return mark < 0 ? [] : row.text.slice(mark + RESPONSE_MARK.length).split("\n");
  }
  return [];
}

/** `text` as a lowercase word stream: markup and punctuation gone, so a
 *  path and its citation compare equal however either was written. */
function wordStream(text: string): string {
  return (text.toLowerCase().match(/[\w']+/g) ?? []).join(" ");
}

/** True when an adjudication `line` names the finding: by its path under
 *  either spelling — matched on whole path tokens, so `tests/config.py`
 *  never names `config.py` — by its title, or by its first words on
 *  whole words, so `criteria:10` never names `criteria:1`. An explicit
 *  `path:line` citation is the strongest signal: when one spells out the
 *  finding's path, only the cited line's finding is named. */
function namesFinding(line: string, finding: Finding): boolean {
  const parts = findingParts(finding.message);
  const paths = new Set([findingPath(finding), finding.path]);
  const cited = (line.match(CITATION_RE) ?? []).filter((token) => {
    const path = token.slice(0, token.lastIndexOf(":"));
    return paths.has(path) || paths.has(relativePath(path));
  });
  if (cited.length > 0) {
    return cited.some(
      (token) => finding.line == null || Number(token.slice(token.lastIndexOf(":") + 1)) === finding.line,
    );
  }
  for (const token of line.match(PATH_TOKEN_RE) ?? []) {
    if (paths.has(token) || paths.has(relativePath(token))) return true;
  }
  const stream = ` ${wordStream(line)} `;
  const needles: string[] = [];
  if (parts.title != null) {
    needles.push(wordStream(parts.title));
    const criterion = /^criterion\s+(\d+)/i.exec(parts.title);
    if (criterion) needles.push(`criterion ${criterion[1]}`);
  }
  const basis = parts.title ?? (parts.body !== "" ? parts.body : finding.message);
  const first = wordStream(basis).split(" ").filter(Boolean).slice(0, 5).join(" ");
  needles.push(first);
  return needles.some((needle) => needle.length >= 3 && stream.includes(` ${needle} `));
}

/** The adjudication `lines` pass on `finding`: the first DECLINE or
 *  FOLLOW_UP line naming it, as its fate and the sentence itself. */
function adjudicated(lines: string[], finding: Finding): { fate: Fate; sentence: string } | null {
  for (const raw of lines) {
    const verdict = ADJUDICATION_RE.exec(raw);
    if (verdict == null || !namesFinding(raw, finding)) continue;
    return {
      fate: verdict[1]!.toLowerCase().startsWith("decline") ? "declined" : "follow_up",
      sentence: raw.trim(),
    };
  }
  return null;
}

/** Every round's findings with what became of each, newest round first.
 *
 *  The fate comes from what the store already holds: a DECLINE or
 *  FOLLOW_UP line in the round's ledger row (the `Implementer response`
 *  under `Round N: REQUEST_CHANGES -> fix round`) that names the finding
 *  is the implementer adjudicating it; absent that, a key missing from
 *  the next round means the fix round resolved it; anything left was
 *  still open — in the last round, when the run ended. */
export function findingsHistory(rounds: Round[], ledger: LedgerRow[]): RoundHistory[] {
  const ordered = [...rounds].sort((a, b) => a.round - b.round);
  const keys = ordered.map((round) => new Set(round.findings.map(findingKey)));
  return ordered
    .map((round, index) => {
      const lines = responseLines(ledger, round.round);
      const next = keys[index + 1];
      const findings = round.findings
        .map((finding): HistoryFinding => {
          const judged = adjudicated(lines, finding);
          if (judged != null) return { finding, fate: judged.fate, sentence: judged.sentence };
          return { finding, fate: next != null && !next.has(findingKey(finding)) ? "fixed" : "open", sentence: null };
        })
        .sort((a, b) => RANK[severityOf(a.finding)] - RANK[severityOf(b.finding)]);
      return { round: round.round, findings };
    })
    .reverse();
}
