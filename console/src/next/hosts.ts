import { age, formatClock } from "../lib/format";
import { baseOf, buildLine, byDaemon, hostName, SWEEP_OK, type HostRecord } from "../lib/hosts";
import type { ContractBodies } from "./fleet";
import { useConsolePeers } from "./scope";

type Record = HostRecord<ContractBodies>;
type Tone = "live" | "need" | "bad" | "ink-3";
const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
const sweepWords = { fresh: "OK", running: "Running", stale: "Stale", killed: "Killed", none: "Never ran", unreadable: "Unreadable" };

function projectsOf(records: Record[]) {
  const first = records[0]!;
  if (first.host_status) return first.host_status.projects.map((project) => ({
    name: project.name ?? project.path,
    admission: project.admission,
    error: project.error,
    supervisor: project.supervisor,
    runs: project.runs.length,
    workers: records.find((record) => record.name === project.name)?.status?.workers ?? 0,
  }));
  const status = first.status;
  return status ? [{
    name: status.project.split("/").filter(Boolean).pop() ?? status.project,
    admission: status.admission,
    error: null,
    supervisor: status.supervisor,
    runs: status.runs.length,
    workers: status.workers,
  }] : [];
}

function healthOf(first: Record, down: boolean, now: number): { text: string; tone: Tone; glyph: string } {
  const seen = first.root_seen_ms ?? first.seen_ms;
  if (down) return { text: `Not answering · ${seen == null ? "never answered" : `last seen ${age(now - seen)} ago`}`, tone: "need", glyph: "△" };
  if (first.needs_token && (first.root_failed || !first.host_status)) return { text: "Asks for its serve token", tone: "ink-3", glyph: "" };
  const errors = first.host_status?.projects.filter((project) => project.error != null) ?? [];
  if (errors.length) return { text: errors.length === 1 ? `${errors[0]!.name ?? errors[0]!.path} cannot be read` : `${errors.length} projects cannot be read`, tone: "bad", glyph: "△" };
  const sweep = first.host_status?.sweep;
  if (sweep && !SWEEP_OK.has(sweep.state)) return { text: `Sweep ${sweepWords[sweep.state].toLowerCase()}`, tone: "bad", glyph: "△" };
  return { text: "Healthy", tone: "live", glyph: "✓" };
}

function daemonCard(records: Record[], now: number) {
  const first = records[0]!;
  const root = first.host_status;
  const body = root ?? first.status;
  const down = !first.needs_token && (first.root_failed === true || (!root && first.error != null));
  const projects = projectsOf(records);
  const beats = projects.flatMap((project) => project.supervisor?.heartbeat_age_ms == null ? [] : [project.supervisor.heartbeat_age_ms]);
  const busy = `${projects.reduce((sum, project) => sum + project.runs, 0)} of ${projects.reduce((sum, project) => sum + project.workers, 0)} workers busy`;
  const supervisor = projects.some((project) => project.supervisor?.state === "stale") ? "Stale"
    : projects.length > 0 && projects.every((project) => project.supervisor?.state === "live") ? "Running"
      : projects.some((project) => project.supervisor?.state === "live") ? "Partly running" : "Not running";
  const seenMs = first.root_seen_ms ?? first.seen_ms;
  const seen = seenMs == null ? null : formatClock(seenMs);
  const sweep = root?.sweep;
  const sweepWord = sweep ? sweepWords[sweep.state] : "No host sweep on a project daemon";
  const lastSweep = sweep?.ended == null ? "never" : down ? formatClock(sweep.ended) : `${age(body!.now - sweep.ended)} ago`;
  return {
    key: first.address, name: hostName(first), address: baseOf(first.address), down, seen,
    health: healthOf(first, down, now), projects,
    build: root?.build.daemon?.slice(0, 7) ?? "—",
    checkout: root && buildLine(root.build).differ ? root.build.head?.slice(0, 7) : null,
    uptime: !down && body?.daemon ? age(body.now - body.daemon.started_ms) : "unknown",
    lastSweep,
    supervisor: down ? "Unknown" : supervisor,
    supervisorTone: (down ? "ink-3" : supervisor === "Stale" ? "bad" : supervisor === "Running" ? "live" : "ink-3") as Tone,
    supervisorNote: down ? (seen ? `last report ${seen}: ${supervisor.toLowerCase()}, ${busy}` : "No last report") : `${busy}${beats.length ? ` · beat ${age(Math.max(...beats))} ago` : ""}`,
    sweep: down ? "Unknown" : sweepWord,
    sweepTone: (down || !sweep || sweep.state === "none" ? "ink-3" : SWEEP_OK.has(sweep.state) ? "live" : "bad") as Tone,
    sweepNote: down ? (seen ? `last report ${sweep?.ended == null ? seen : formatClock(sweep.ended)}: ${sweepWord.toLowerCase()}` : "No last report")
      : sweep ? `last ${sweep.ended == null ? "never" : formatClock(sweep.ended)}${sweep.exit != null && sweep.exit !== 0 ? ` · exit ${sweep.exit}` : ""}` : "",
  };
}

export function useHostsTab() {
  const { hosts, now, polls, polledAgo } = useConsolePeers();
  const daemons = byDaemon(hosts).map((records) => daemonCard(records, now));
  const answering = daemons.filter((daemon) => !daemon.down).length;
  return {
    daemons, allDown: daemons.length > 0 && answering === 0,
    summary: polls === 0 ? "asking…" : `${plural(daemons.length, "daemon")} · ${answering || "none"} answering · polled ${age(polledAgo ?? 0)} ago`,
  };
}

export type HostCard = ReturnType<typeof useHostsTab>["daemons"][number];
