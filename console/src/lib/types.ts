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
