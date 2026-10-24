import { useContext } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useSearch } from "@tanstack/react-router";
import { scoutRoutesSchema, scoutsSchema, type ScoutRoute, type ScoutRow, type ScoutRoutes, type Scouts } from "../lib/contract.gen";
import { projectName } from "../lib/derive";
import type { HostRecord } from "../lib/hosts";
import { POLL_INTERVAL_MS } from "../lib/poll";
import { prLabel } from "../lib/shipped";
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

type ScoutKind = ScoutRow["kind"];

const SCOUT_KINDS: ScoutKind[] = ["bug", "research", "recon"];

/** The Scouts page's search: `new` opens the New scout form for that
 *  project, filled from `about`, `kind` and `ask`. */
export interface ScoutsSearch {
  new?: string;
  about?: string;
  kind?: ScoutKind;
  ask?: string;
}

const text = (value: unknown) => (typeof value === "string" ? value : undefined);

export function validateScoutsSearch(search: Record<string, unknown>): ScoutsSearch {
  const kind = SCOUT_KINDS.find((each) => each === search.kind);
  return { new: text(search.new), about: text(search.about), kind, ask: text(search.ask) };
}

/** What a Scout button opens the form about: `about` is the launch's
 *  `subject`, `off` the title of a button drawn disabled. */
export interface ScoutTarget {
  project: string;
  about: string;
  kind: ScoutKind;
  ask: string;
  off: string | null;
}

/** The subject `about` names, in words: "run 812", "the defect in run 812",
 *  "DEMO-7" or a pull request's label; null for a subject of no known kind. */
export function aboutWords(about: string): string | null {
  const split = about.indexOf(":");
  if (split < 0) return null;
  const ref = about.slice(split + 1);
  if (ref === "") return null;
  switch (about.slice(0, split)) {
    case "run":
      return `run ${ref}`;
    case "defect":
      return `the defect in run ${ref}`;
    case "ticket":
      return ref;
    case "pr":
      return prLabel(ref);
    default:
      return null;
  }
}

/** Where a scout's subject `about` lives in the console, or the pull
 *  request's own address; null where `aboutWords` has no words for it. */
export type AboutTarget =
  | { words: string; run: string }
  | { words: string; ticket: string }
  | { words: string; url: string };

export function aboutTarget(about: string): AboutTarget | null {
  const words = aboutWords(about);
  if (words == null) return null;
  const kind = about.slice(0, about.indexOf(":"));
  const ref = about.slice(kind.length + 1);
  if (kind === "pr") return { words, url: ref };
  return kind === "ticket" ? { words, ticket: ref } : { words, run: ref };
}
