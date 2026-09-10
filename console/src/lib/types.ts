/** The daemon's `/status` body (holophyte/serve.py `status()`). `project`
 *  and `daemon` arrive with the daemon field ticket, so both are optional. */
export interface Status {
  target: string;
  project?: string;
  host: string;
  now: number;
  daemon?: { started_ms: number; pid: number };
  supervisor: Supervisor;
  thresholds: { heartbeat_stale_ms: number; strikes: number };
  /** Whether `[serve] actions = true` opened the `POST /actions/...`
   *  routes; a daemon older than the field sends none, read as false. */
  actions?: boolean;
  runs: Run[];
}

export interface Supervisor {
  state: "live" | "stale" | "none";
  pid: number | null;
  heartbeat_age_ms: number | null;
  host: string | null;
}

export interface Run {
  id: number;
  ticket: string;
  phase: string;
  heartbeat_age_ms: number;
  elapsed_ms: number;
  time_box_ms: number;
  host: string;
  title?: string;
  started_ms?: number;
  round?: number;
  strikes?: number;
}

/** The daemon's `/attention` body (holophyte/serve.py `attention()`). */
export interface Attention {
  level: "none" | "working" | "attention" | "critical";
  now: number;
  items: AttentionItem[];
}

export interface AttentionItem {
  kind: string;
  level: string;
  /** On an item that names a run: the pull request it opened, else null. */
  pr_url?: string | null;
  [key: string]: unknown;
}

/** One review finding as the daemon decodes it from `reviewRounds.findings`. */
export interface Finding {
  path: string;
  line?: number | null;
  severity: string;
  criterion?: string;
  message: string;
}

/** One review round of `/runs/N`, oldest first on the wire. */
export interface Round {
  round: number;
  started_ms: number;
  ended_ms: number | null;
  verdict: "pass" | "changes_requested" | "error" | string;
  reviewer_model?: string | null;
  findings: Finding[];
}

/** The daemon's `/runs/N` body (holophyte/serve.py `run_detail()`). */
export interface RunDetailBody {
  run: {
    id: number;
    ticket: string;
    title?: string | null;
    phase: string;
    attempt?: number;
    started_ms: number;
    ended_ms: number | null;
    outcome?: string | null;
    time_box_ms: number;
    branch?: string | null;
    host: string | null;
    heartbeat_age_ms?: number | null;
    merge_sha?: string | null;
    /** The merge commit's page on origin when the sha has reached it, else null. */
    commit_url?: string | null;
    /** The pull request the run opened under PR mode (`runs.prUrl`), else null. */
    pr_url?: string | null;
    /** The loop's review-round cap; a body without it falls back to the rounds seen. */
    max_rounds?: number;
  };
  rounds: Round[];
  events: RunEvent[];
}

/** One narrative run event of `/runs/N` (`runEvents` with `level = narrative`). */
export interface RunEvent {
  at: number;
  kind: string;
  summary: string;
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

/** One merged run of `/shipped` (holophyte/serve.py `shipped()`), newest
 *  end first on the wire. `estimate_min` is null for a run with no box. */
export interface ShippedRow {
  id: number;
  ticket: string;
  title: string | null;
  rounds: number;
  findings: number;
  started_ms: number;
  ended_ms: number;
  actual_min: number;
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
 *  right the path to merge (holophyte/serve.py `BOARD_STATES`). */
export type BoardState = "needs_spec" | "blocked_on_deps" | "ready" | "blocked_on_operator" | "in_flight";

/** One open ticket of `/board` (holophyte/serve.py `board()`): `run` is
 *  the live run's id or null, `question` the blocked question or null,
 *  `waits_on` the open tickets its dependencies name. */
export interface BoardWireTicket {
  ticket: string;
  title: string | null;
  time_box_ms: number | null;
  run: number | null;
  question: string | null;
  waits_on: string[];
  mirrored_ms: number | null;
}

/** The daemon's `/board` body: every column present, empty or not, in
 *  path order. */
export interface BoardBody {
  columns: { state: BoardState; tickets: BoardWireTicket[] }[];
  now: number;
}
