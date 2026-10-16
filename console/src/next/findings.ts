import { useContext } from "react";
import { useSearch } from "@tanstack/react-router";
import type { AdvisoryFinding, Findings, FindingsRun, FollowUpRow, Status } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import { findingKey, findingsHistory, severityOf, type HistoryFinding } from "../lib/findings";
import type { HostRecord } from "../lib/hosts";
import type { LedgerRow } from "../lib/ledger";
import type { Round } from "../lib/types";
import type { ContractBodies } from "./fleet";
import { useFindings, weekSince } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";
import { stateOf } from "./state";

/** Why nobody is fixing an open finding: its ticket's run is stale on
 *  `/status`, or the run that raised it ended failed or paused, or no run
 *  of it is live. */
export type Idle = "stale" | "failed" | "paused" | "not_running";

interface TaggedFinding {
  project: string;
  ticket: string;
  run: number;
  entry: HistoryFinding;
}

export interface OpenFinding extends TaggedFinding {
  /** Null while a live run of the ticket is on it. */
  idle: Idle | null;
}

type FollowUp =
  | { project: string; kind: "row"; row: FollowUpRow }
  | { project: string; kind: "advisory"; ticket: string; run: number; advisory: AdvisoryFinding };

interface CleanReview {
  project: string;
  ticket: string;
  run: number;
  round: number;
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

/** Each finding of `run` once, at the newest round in the window that raised it. */
function newestEntries(run: FindingsRun, since: number): HistoryFinding[] {
  const rounds = new Set(run.rounds.filter((round) => inWindow(round, since)).map((round) => round.round));
  const seen = new Set<string>();
  const entries: HistoryFinding[] = [];
  for (const history of findingsHistory(run.rounds, ledgerRows(run))) {
    if (!rounds.has(history.round)) continue;
    for (const entry of history.findings) {
      const key = findingKey(entry.finding);
      if (seen.has(key)) continue;
      seen.add(key);
      entries.push(entry);
    }
  }
  return entries;
}

function idleOf(run: FindingsRun, status: Status | undefined): Idle | null {
  const staleMs = status?.thresholds.heartbeat_stale_ms ?? Infinity;
  const live = (status?.runs ?? []).filter((each) => each.ticket === run.ticket).map((each) => stateOf({ run: each, staleMs }));
  if (live.includes("running")) return null;
  if (live.includes("stale")) return "stale";
  const ended = stateOf({ run, staleMs: Infinity });
  if (ended === "failed" || ended === "paused") return ended;
  return "not_running";
}

const RANK = { must: 0, should: 1, nit: 2 };
const worstFirst = (a: { entry: HistoryFinding }, b: { entry: HistoryFinding }) =>
  RANK[severityOf(a.entry.finding)] - RANK[severityOf(b.entry.finding)];

function addHost(page: FindingsPage, host: Host, answer: Findings, since: number): void {
  const project = host.project == null ? host.label : projectName(host.project);
  const status = host.contract_error ? undefined : (host.status ?? undefined);
  for (const run of answer.runs) {
    const windowed = run.rounds.filter((round) => inWindow(round, since));
    page.reviews += windowed.length;
    for (const round of windowed) {
      if (round.verdict === "pass" && round.findings.length === 0) page.clean.push({ project, ticket: run.ticket, run: run.run, round: round.round });
    }
    const merged = run.outcome === "merged";
    const entries = newestEntries(run, since);
    page.findings += entries.length;
    for (const entry of entries) {
      const tagged = { project, ticket: run.ticket, run: run.run, entry };
      if (entry.fate === "follow_up") continue;
      if (entry.fate === "open" && !merged) page.open.push({ ...tagged, idle: idleOf(run, status) });
      else page.settled.push(tagged);
    }
    for (const advisory of run.advisories) page.followUps.push({ project, kind: "advisory", ticket: run.ticket, run: run.run, advisory });
  }
  for (const row of answer.follow_ups) {
    page.followUps.push({ project, kind: "row", row });
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
  return page;
}
