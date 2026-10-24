import { useContext } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { scoutRoutesSchema, scoutsSchema, type ScoutRoute, type ScoutRow, type ScoutRoutes, type Scouts } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";
import { POLL_INTERVAL_MS } from "../lib/poll";
import type { ContractBodies } from "./fleet";
import { routeKey, routeOrNone } from "./queries";
import { PollDepsContext, scopedHosts, useConsolePeers } from "./scope";

export interface ScoutListRow extends ScoutRow {
  base: string;
  project: string;
}

export type ScoutState = "running" | "queued" | "done" | "stopped" | "failed";

export function scoutState(phase: ScoutRow["phase"]): ScoutState {
  return phase === "queued" || phase === "done" || phase === "stopped" || phase === "failed" ? phase : "running";
}

/** Every answer's scouts, tagged with their host's base and project,
 *  newest first and then by id. */
function scoutRows(answers: { host: HostRecord<ContractBodies>; scouts: Scouts }[]): ScoutListRow[] {
  return answers
    .flatMap(({ host, scouts }) => {
      const project = projectName(host.status?.project ?? host.project ?? host.label);
      return scouts.scouts.map((scout) => ({ ...scout, base: host.base, project }));
    })
    .sort((a, b) => b.created_ms - a.created_ms || b.id - a.id);
}

export function countScouts(rows: ScoutRow[], state: ScoutState): number {
  return rows.filter((row) => scoutState(row.phase) === state).length;
}

/** The scoped hosts' `/scouts`, one query per host shared by every reader;
 *  a daemon without the route adds no rows. */
export function useScouts(): { project: string | undefined; rows: ScoutListRow[] } {
  const deps = useContext(PollDepsContext);
  const { hosts } = useConsolePeers();
  const { project } = useSearch({ from: "__root__" });
  const asked = scopedHosts(hosts, project).filter((host) => host.status != null);
  const results = useQueries({
    queries: asked.map((host) => ({
      queryKey: routeKey(host.base, "/scouts"),
      queryFn: () => routeOrNone(deps.fetch, `${host.base}/scouts`, scoutsSchema),
      refetchInterval: POLL_INTERVAL_MS,
      staleTime: POLL_INTERVAL_MS,
    })),
  });
  const answers = asked.flatMap((host, index) => {
    const scouts = results[index]?.data;
    return scouts ? [{ host, scouts }] : [];
  });
  return { project, rows: scoutRows(answers) };
}

/** `/scouts/routes` at `base`, read again whenever a reader asks for it;
 *  null from a daemon without the route, idle with no base. */
export function useScoutRoutes(base: string | null) {
  const deps = useContext(PollDepsContext);
  return useQuery({
    queryKey: routeKey(base ?? "", "/scouts/routes"),
    queryFn: () => routeOrNone(deps.fetch, `${base}/scouts/routes`, scoutRoutesSchema),
    enabled: base != null,
  });
}

/** The route `named` when `routes` lists it, else the first listed. */
function pickRoute(routes: ScoutRoute[], named: string): string | null {
  return (routes.find((route) => route.name === named) ?? routes[0])?.name ?? null;
}

export interface ScoutPicks {
  lead: string | null;
  helper: string | null;
  swarm: string;
  minutes: string;
}

export function defaultPicks(routes: ScoutRoutes): ScoutPicks {
  const { defaults } = routes;
  return {
    lead: pickRoute(routes.leads, defaults.lead),
    helper: pickRoute(routes.helpers, defaults.helper),
    swarm: String(defaults.swarm),
    minutes: String(defaults.minutes),
  };
}

/** `text` as a whole number from `least` to `most`, else null. */
export function wholeIn(text: string, least: number, most = Number.POSITIVE_INFINITY): number | null {
  if (!/^\s*\d+\s*$/.test(text)) return null;
  const value = Number(text);
  return value >= least && value <= most ? value : null;
}

/** The daemon's `detail` as a sentence: a capital first letter and a stop. */
export function sentence(detail: string): string {
  const text = detail.trim();
  if (text === "") return text;
  const capital = text[0]!.toUpperCase() + text.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}
