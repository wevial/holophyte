import type { z } from "zod";
import type { hostProjectSchema, hostStatusSchema, statusSchema, supervisorSchema, sweepSchema, runSchema, findingSchema, roundSchema, runDetailSchema, runEventSchema } from "./schemas";

/** The daemon's `/attention` body (holophyte/serve.py `attention()`). */
export interface Attention {
  level: "none" | "working" | "attention" | "critical";
  now: number;
  items: AttentionItem[];
}

export interface AttentionItem {
  ticket_url?: string | null;
  kind: string;
  level: string;
  /** On an item that names a run: the pull request it opened, else null. */
  pr_url?: string | null;
  [key: string]: unknown;
}

/** One file of `/runs/N/files`: git's status letter and its line counts. */
export interface TouchedFile {
  path: string;
  status: string;
  added: number;
  deleted: number;
}

/** The daemon's `/runs/N/files` body (holophyte/serve.py `run_files()`). */
export interface RunFilesBody {
  run?: number;
  base?: string;
  head?: string;
  files: TouchedFile[];
  total_added: number;
  total_deleted: number;
  truncated?: boolean;
}

/** One finished run of `/shipped` (holophyte/serve.py `shipped()`), newest
 *  end first on the wire. `estimate_min` is null for a run with no box. */
export interface ShippedRow {
  /** Absent on older daemons, whose ledger contains only merges. */
  outcome?: string;
  outcome_reason?: string | null;
  id: number;
  ticket: string;
  ticket_url?: string | null;
  title: string | null;
  rounds: number;
  findings: number;
  started_ms: number;
  ended_ms: number;
  actual_min: number | null;
  working_ms?: number | null;
  /** The two parts of `working_ms`; absent on a daemon older than them. */
  agent_ms?: number | null;
  verify_ms?: number | null;
  wall_min?: number;
  estimate_min: number | null;
  merge_sha: string | null;
  /** The merge commit's page on the repository's origin when the sha has
   *  reached `origin/main`, else null (holophyte/serve.py `commit_url()`). */
  commit_url: string | null;
  /** The pull request the run merged through under PR mode (`runs.prUrl`),
   *  else null; a daemon older than the field sends none. */
  pr_url?: string | null;
  host: string | null;
  /** The console's stamp: the base of the daemon the row came from. */
  daemon?: string;
  /** The console's stamp: the name of the project the daemon serves,
   *  from its `/status` (`projectName()`), so the Project column tells
   *  two daemons' merges apart where `host` is the same on every row. */
  project: string;
}

/** A `/shipped` row as the daemon sends it, before the console's stamps. */
export type ShippedWireRow = Omit<ShippedRow, "daemon" | "project">;

/** The daemon's `/shipped` body: one page and the cursor for the next,
 *  null on the last page. */
export interface ShippedBody {
  rows: ShippedWireRow[];
  limit: number;
  before?: number | null;
  next_before?: number | null;
}

/** The open ticket states, in the order `/board` answers them: left to
 *  right the path to merge (holophyte/serve.py `BOARD_STATES`), after
 *  the `backlog` column a native project answers first. */
export type BoardState = "backlog" | "needs_spec" | "blocked_on_deps" | "ready" | "blocked_on_operator" | "in_flight";

/** One open ticket of `/board` (holophyte/serve.py `board()`): `run` is
 *  the live run's id or null, `question` the blocked question or null,
 *  `waits_on` the open tickets its dependencies name. */
export interface BoardWireTicket {
  ticket: string;
  ticket_url?: string | null;
  title: string | null;
  time_box_ms: number | null;
  run: number | null;
  question: string | null;
  waits_on: string[];
  mirrored_ms: number | null;
}

/** The daemon's `/board` body: every column present, empty or not, in
 *  path order. `editable` is true only for a native project on a host
 *  daemon with actions on, where the console may file; a daemon older
 *  than the field sends none. */
export interface BoardBody {
  columns: { state: BoardState; tickets: BoardWireTicket[] }[];
  now: number;
  editable?: boolean;
}

export type Status = z.infer<typeof statusSchema>;
export type HostStatus = z.infer<typeof hostStatusSchema>;
export type HostProject = z.infer<typeof hostProjectSchema>;
export type Sweep = z.infer<typeof sweepSchema>;
export type Supervisor = z.infer<typeof supervisorSchema>;
export type Run = z.infer<typeof runSchema>;
export type Finding = z.infer<typeof findingSchema>;
export type Round = z.infer<typeof roundSchema>;
export type RunDetailBody = z.infer<typeof runDetailSchema>;
export type RunEvent = z.infer<typeof runEventSchema>;
