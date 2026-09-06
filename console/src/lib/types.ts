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
