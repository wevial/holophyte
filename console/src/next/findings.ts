import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import type { AdvisoryFinding, Findings, FindingsRun, FollowUpRow, Status, StatusRun } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { findingKey, findingParts, findingPath, findingsHistory, severityOf, type HistoryFinding, type Severity } from "../lib/findings";
import type { HostRecord } from "../lib/hosts";
import type { LedgerRow } from "../lib/ledger";
import { phaseLabel } from "../lib/runs";
import { prLabel } from "../lib/shipped";
import type { Finding, Round } from "../lib/types";
import { classicHref } from "../lib/view";
import { reviewRound } from "./components/StageChip";
import type { ContractBodies } from "./fleet";
import { useFindings, weekSince } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { stateOf, type State, type Tone } from "./state";

/** Why nobody is fixing an open finding: its ticket's run is stale on
 *  `/status`, or the run that raised it ended failed or paused, or no run
 *  of it is live. */
export type Idle = "stale" | "failed" | "paused" | "not_running";

/** What became of a row, as its icon and its one fate line. */
interface RowFate {
  state: State;
  /** The icon's accessible name. */
  word: string;
  line: string;
  tone: Tone;
}

/** One folding row of the page, built from a finding, a follow-up or an advisory thread. */
export interface FindingRowView {
  key: string;
  severity: Severity | null;
  tag: { text: string; filled: boolean } | null;
  title: string;
  text: string;
  project: string;
  path: string | null;
  place: string;
  fate: RowFate;
  /** The classic run view, on finding rows only. */
  href: string | null;
}

interface TaggedFinding {
  project: string;
  ticket: string;
  run: number;
  entry: HistoryFinding;
  /** The round that raised it, and when that round ended. */
  round: number;
  at: number;
  row: FindingRowView;
}

export interface OpenFinding extends TaggedFinding {
  /** Null while a live run of the ticket is on it. */
  idle: Idle | null;
}

interface FollowUp {
  pending: boolean;
  at: number;
  row: FindingRowView;
}

interface CleanReview {
  project: string;
  ticket: string;
  run: number;
  round: number;
  reviewer: string | null;
}

export interface FindingsPage {
  project: string | undefined;
  /** Whether some scoped host has answered `/findings`, or lacks it. */
  answered: boolean;
  reviews: number;
  findings: number;
  /** Pending `feature` follow-ups: each a Backlog draft once its PR merges. */
  drafts: number;
  open: OpenFinding[];
  followUps: FollowUp[];
  settled: TaggedFinding[];
  clean: CleanReview[];
}

type Host = HostRecord<ContractBodies>;

const inWindow = (round: Round, since: number) => (round.ended_ms ?? round.started_ms) >= since;

function ledgerRows(run: FindingsRun): LedgerRow[] {
  return run.ledger.map((row) => ({ ...row, run: run.run, ticket: run.ticket }));
}

interface Entry {
  entry: HistoryFinding;
  round: number;
  at: number;
  /** The round after the one that raised it, where a fixed finding was gone. */
  next: number | null;
}

/** Each finding of `run` once, at the newest round in the window that raised it. */
function newestEntries(run: FindingsRun, since: number): Entry[] {
  const windowed = new Map(run.rounds.filter((round) => inWindow(round, since)).map((round) => [round.round, round]));
  const numbers = run.rounds.map((round) => round.round).sort((a, b) => a - b);
  const seen = new Set<string>();
  const entries: Entry[] = [];
  for (const history of findingsHistory(run.rounds, ledgerRows(run))) {
    const round = windowed.get(history.round);
    if (round == null) continue;
    const next = numbers.find((number) => number > history.round) ?? null;
    for (const entry of history.findings) {
      const key = findingKey(entry.finding);
      if (seen.has(key)) continue;
      seen.add(key);
      entries.push({ entry, round: round.round, at: round.ended_ms ?? round.started_ms, next });
    }
  }
  return entries;
}

function liveRunsOf(ticket: string, status: Status | undefined): { run: StatusRun; state: State }[] {
  const staleMs = status?.thresholds.heartbeat_stale_ms ?? Infinity;
  return (status?.runs ?? []).filter((each) => each.ticket === ticket).map((each) => ({ run: each, state: stateOf({ run: each, staleMs }) }));
}

function idleOf(run: FindingsRun, live: { state: State }[]): Idle | null {
  if (live.some((each) => each.state === "running")) return null;
  if (live.some((each) => each.state === "stale")) return "stale";
  const ended = stateOf({ run, staleMs: Infinity });
  if (ended === "failed" || ended === "paused") return ended;
  return "not_running";
}

const RANK = { must: 0, should: 1, nit: 2 };
const worstFirst = (a: TaggedFinding, b: TaggedFinding) =>
  RANK[severityOf(a.entry.finding)] - RANK[severityOf(b.entry.finding)] || b.at - a.at || b.round - a.round;
const pendingFirst = (a: FollowUp, b: FollowUp) => Number(b.pending) - Number(a.pending) || b.at - a.at;

const IDLE_WORDS: Record<Idle, string> = {
  stale: "the run is stale",
  failed: "the run failed",
  paused: "the run is paused",
  not_running: "the ticket has no run",
};

// A DECLINE line's lead: its bullet, the word, a cited `path:line` and the dash before the reason.
const DECLINE_LEAD_RE = /^\s*(?:(?:[-*+]|\d+[.)])\s+)?DECLINE\b\s*:?\s*(?:[\w./-]+:\d+\s*)?(?:[-–—:]+\s*)?/i;

function openFate(idle: Idle | null, live: StatusRun | undefined): RowFate {
  if (idle != null) {
    return { state: "needs_you", word: "open, nobody fixing it", line: `open · ${IDLE_WORDS[idle]}, so nobody is fixing it`, tone: "ask" };
  }
  const round = live == null ? null : reviewRound({ phase: live.phase, round: live.round + 1, max_rounds: live.max_rounds });
  const doing = live == null ? "" : ` · ${phaseLabel(live.phase)}${round == null ? "" : `, ${round}`}`;
  return { state: "running", word: "being fixed", line: `being fixed${doing}`, tone: "live" };
}

function settledFate(found: Entry, merged: boolean): RowFate {
  const { entry } = found;
  if (entry.fate === "declined") {
    const reason = (entry.sentence ?? "").replace(DECLINE_LEAD_RE, "").trim();
    return { state: "failed", word: "declined", line: reason === "" ? "declined" : `declined · ${reason}`, tone: "ink-2" };
  }
  if (entry.fate === "open" && merged) return { state: "needs_you", word: "merged open", line: "merged with this finding open", tone: "ink-2" };
  return { state: "ready_to_merge", word: "fixed", line: `fixed in round ${found.next ?? found.round + 1}`, tone: "ink-2" };
}

function followUpFate(row: FollowUpRow, run: FindingsRun | undefined): RowFate {
  switch (row.state) {
    case "pending": {
      const waiting = { state: "queued", word: "follow-up", tone: "ink-2" } as const;
      if (row.kind === "guardrail") return { ...waiting, line: `joins the findings ledger when ${row.ticket} merges` };
      const when = run?.pr_url ? prLabel(run.pr_url) : row.ticket;
      return { ...waiting, line: `becomes a Backlog draft when ${when} merges` };
    }
    case "filed":
      return { state: "ready_to_merge", word: "filed", line: `filed as ${row.filed_as ?? "a ticket"} · Backlog`, tone: "live" };
    case "duplicate":
      return { state: "ready_to_merge", word: "already filed", line: `already in Backlog as ${row.duplicate_of ?? "a ticket"}`, tone: "ink-2" };
    case "ledger":
      return { state: "ready_to_merge", word: "in the ledger", line: `added to the findings ledger when ${row.ticket} merged`, tone: "ink-2" };
    case "superseded":
      return { state: "abandoned", word: "superseded", line: "superseded by a later fix commit", tone: "ink-2" };
    case "unfiled":
      return { state: "failed", word: "not filed", line: row.error ? `not filed: ${row.error}` : "not filed", tone: "bad" };
  }
}

/** The bold lead, else the text's first sentence, over the text. */
function titled(message: string): { title: string; text: string } {
  const { title, body } = findingParts(message);
  if (title != null) return { title, text: body };
  const sentence = /^[\s\S]*?[.!?](?=\s|$)/.exec(body.trim());
  return { title: (sentence?.[0] ?? body).trim(), text: body };
}

const placeOf = (path: string, line: number | null | undefined) => (line == null ? path : `${path}:${line}`);

function findingRow(project: string, base: string, run: FindingsRun, found: Entry, fate: RowFate): FindingRowView {
  const { finding } = found.entry;
  const path = findingPath(finding);
  return {
    key: `${base} ${run.run} ${findingKey(finding)}`,
    severity: severityOf(finding),
    tag: finding.criterion ? { text: finding.criterion, filled: false } : null,
    ...titled(finding.message),
    project,
    path,
    place: `${placeOf(path, finding.line)} · ${run.ticket} · round ${found.round}`,
    fate,
    href: classicHref(`#run=${run.run}&daemon=${encodeURIComponent(base)}`),
  };
}

/** The finding a follow-up came from, when its run's rounds still hold it. */
function sourceOf(row: FollowUpRow, run: FindingsRun | undefined): Finding | undefined {
  if (row.path == null) return undefined;
  const findings = (run?.rounds ?? []).flatMap((round) => round.findings);
  return findings.find((finding) => findingPath(finding) === row.path && (row.line == null || finding.line === row.line));
}

function followUpRow(project: string, base: string, row: FollowUpRow, run: FindingsRun | undefined): FindingRowView {
  const source = sourceOf(row, run);
  return {
    key: `${base} follow-up ${row.id}`,
    severity: source == null ? null : severityOf(source),
    tag: { text: row.kind, filled: true },
    ...titled(row.text),
    project,
    path: row.path,
    place: row.path == null ? row.ticket : `${placeOf(row.path, row.line)} · ${row.ticket}`,
    fate: followUpFate(row, run),
    href: null,
  };
}

function advisoryRow(project: string, base: string, run: FindingsRun, advisory: AdvisoryFinding, index: number): FindingRowView {
  const pr = run.pr_url ? `${prLabel(run.pr_url)} · ` : "";
  return {
    key: `${base} ${run.run} advisory ${index}`,
    severity: null,
    tag: { text: "bot thread", filled: true },
    ...titled(advisory.message),
    project,
    path: null,
    place: `${pr}${run.ticket} · advisory`,
    fate: { state: "queued", word: "advisory", line: "noted, not acted on", tone: "wait" },
    href: null,
  };
}

const newestRound = (run: FindingsRun) => Math.max(0, ...run.rounds.map((round) => round.ended_ms ?? round.started_ms));

function addHost(page: FindingsPage, host: Host, answer: Findings, since: number): void {
  const project = host.project == null ? host.label : projectName(host.project);
  const status = host.contract_error ? undefined : (host.status ?? undefined);
  for (const run of answer.runs) {
    const windowed = run.rounds.filter((round) => inWindow(round, since));
    page.reviews += windowed.length;
    for (const round of windowed) {
      if (round.verdict === "pass" && round.findings.length === 0) {
        page.clean.push({ project, ticket: run.ticket, run: run.run, round: round.round, reviewer: round.reviewer_model });
      }
    }
    const merged = run.outcome === "merged";
    const entries = newestEntries(run, since);
    page.findings += entries.length;
    const live = liveRunsOf(run.ticket, status);
    for (const found of entries) {
      const { entry } = found;
      if (entry.fate === "follow_up") continue;
      const tagged = { project, ticket: run.ticket, run: run.run, entry, round: found.round, at: found.at };
      if (entry.fate === "open" && !merged) {
        const idle = idleOf(run, live);
        const fixing = live.find((each) => each.state === "running")?.run;
        page.open.push({ ...tagged, idle, row: findingRow(project, host.base, run, found, openFate(idle, fixing)) });
      } else {
        page.settled.push({ ...tagged, row: findingRow(project, host.base, run, found, settledFate(found, merged)) });
      }
    }
    run.advisories.forEach((advisory, index) => {
      page.followUps.push({ pending: false, at: newestRound(run), row: advisoryRow(project, host.base, run, advisory, index) });
    });
  }
  for (const row of answer.follow_ups) {
    const run = answer.runs.find((each) => each.run === row.run);
    page.followUps.push({ pending: row.state === "pending", at: row.created_ms, row: followUpRow(project, host.base, row, run) });
    if (row.kind === "feature" && row.state === "pending") page.drafts += 1;
  }
}

/** The scoped fleet's week of findings, sorted into the page's four groups. */
export function useFindingsPage(): FindingsPage {
  const deps = useContext(PollDepsContext);
  const peers = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const hosts = scopedHosts(peers.hosts, project);
  const { answered, answers } = useFindings(hosts, peers.now, deps);
  const since = weekSince(peers.now);
  const page: FindingsPage = { project, answered, reviews: 0, findings: 0, drafts: 0, open: [], followUps: [], settled: [], clean: [] };
  for (const { host, findings } of answers) addHost(page, host, findings, since);
  page.open.sort(worstFirst);
  page.settled.sort(worstFirst);
  page.followUps.sort(pendingFirst);
  return page;
}
