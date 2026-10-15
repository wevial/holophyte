import type { Attention, ShippedRow, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { peersFetch } from "../harness";

export const DEMO = "http://writer-host:7710";
export const WRITER = "http://writer-host:7711";
export const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const shippedRow = ((await Bun.file(new URL("shipped.json", SERVE)).json()) as { rows: ShippedRow[] }).rows[0]!;
const ready = await Bun.file(new URL("run-merge-ready.json", SERVE)).json();

export const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
export const NOW = new Date(2026, 9, 15, 12, 0).getTime();

type Item = Attention["items"][number];
const served = (await Bun.file(new URL("attention.json", SERVE)).json()) as Attention;
const park = served.items[0]!;
export const blocked: Item = {
  kind: "blocked", level: "attention", ticket: "DEMO-4", ticket_url: null, question: "Which way?", run: 4,
  asked_ms: NOW - (8 * HOUR + 2 * MINUTE), pr_url: null,
};
const failedAt = (run: number, attempt: number, endedMs: number): Item => ({
  kind: "failed", level: "attention", run, ticket: "DEMO-11", ticket_url: null, reason: "verify failed",
  ended_ms: endedMs, attempt, pr_url: null, triage: null,
});
export const failed = [failedAt(11, 1, NOW - 3 * HOUR), failedAt(12, 2, NOW - 2 * HOUR)];
const parkOf = (ticket: string, run: number, park_kind: string): Item => ({ ...park, ticket, run, park_kind, asked_ms: NOW - HOUR } as Item);
export const ciPark = parkOf("DEMO-8", 8, "ci");
export const prPark = parkOf("WRITER-9", 9, "pull_request");

export const run = (id: number, ticket: string) => ({ ...working.runs[0]!, id, ticket, heartbeat_age_ms: 30_000 });
export const queuedOf = (...tickets: string[]) => tickets.map((ticket) => ({ ...working.queued[0]!, ticket }));
export const merged = (id: number, ticket: string, endedMs: number, outcome = "merged"): ShippedRow => ({
  ...shippedRow, id, ticket, ended_ms: endedMs, outcome,
});

interface Daemon {
  items: Item[];
  runs?: Status["runs"];
  queued?: Status["queued"];
  shipped?: ShippedRow[];
}

/** `demo` on DEMO and `writer` on WRITER, with WRITER's run 9 answering its
 *  merge readiness as `writerReady`. */
export function fleet(demo: Daemon, writer: Daemon, writerReady = true): Fetch {
  const daemon = (project: string, body: Daemon) => ({
    status: { ...working, project, runs: body.runs ?? [], queued: body.queued ?? [] },
    attention: { level: body.items.length ? "attention" : "none", now: NOW, project, items: body.items },
  });
  const polls = peersFetch(DEMO, { [DEMO]: daemon("/srv/dev/demo", demo), [WRITER]: daemon("/srv/dev/writer", writer) });
  return async (url) => {
    if (url === `${WRITER}/runs/9/merge`) return Response.json({ ...ready, run: 9, ticket: "WRITER-9", ready: writerReady });
    if (url === `${DEMO}/shipped?limit=50`) return Response.json({ rows: demo.shipped ?? [], next_before: null, limit: 50 });
    if (url === `${WRITER}/shipped?limit=50`) return Response.json({ rows: writer.shipped ?? [], next_before: null, limit: 50 });
    return polls(url);
  };
}

/** DEMO with a blocked ticket, two failed attempts of one ticket and a `ci`
 *  park; WRITER with one `pull_request` park: three Needs you entries. */
export const needsDaemons = (writerReady = true) => fleet({ items: [blocked, ...failed, ciPark] }, { items: [prPark] }, writerReady);
