import type { Status, Supervisor } from "./types";

/** A run is stale strictly past the threshold (the daemon's rule). */
export function isStale(ageMs: number | null | undefined, thresholdMs: number): boolean {
  return ageMs != null && ageMs > thresholdMs;
}

/** The supervisor is stale at the threshold, not past it; the daemon says
 *  so in `state`, and the age is the tie-break for a body without one. */
export function isSupervisorStale(supervisor: Supervisor, thresholdMs: number): boolean {
  if (supervisor.state === "stale") return true;
  return supervisor.heartbeat_age_ms != null && supervisor.heartbeat_age_ms >= thresholdMs;
}

/** The last path segment: `/srv/dev/writer` → `writer`, trailing slash ignored. */
export function projectName(path: string): string {
  const segments = path.split("/").filter((segment) => segment.length > 0);
  return segments.length > 0 ? segments[segments.length - 1]! : path;
}

/** The project row's sub-line: `writer · supervisor live`. */
export function supervisorLabel(status: Status): string {
  return `${status.host} · supervisor ${status.supervisor.state}`;
}
