import { expect, test } from "bun:test";
import type { HostRecord } from "../../src/lib/hosts";
import type { Attention, AttentionItem, Status } from "../../src/lib/types";
import { matchRows, switcherRows } from "../../src/next/projects";
import { fixture, hostOf } from "../harness";
import { ciPark, prPark } from "./needsFleet";

const working = await fixture<Status>("working.json");
const allKinds = await fixture<{ status: Status; attention: Attention }>("attention_all_kinds.json");
const [one, two] = allKinds.attention.items as [AttentionItem, AttentionItem];

function host(base: string, project: string, runs: number, items: AttentionItem[], admission = "enabled"): HostRecord {
  const run = working.runs[0]!;
  const status: Status = {
    ...working,
    project: `/srv/dev/${project}`,
    admission,
    runs: Array.from({ length: runs }, (_, at) => ({ ...run, id: run.id + at })),
  };
  return hostOf(status, { level: items.length ? "attention" : "none", now: 0, items }, base);
}

const NO_READINESS = new Map<string, { ready: boolean }>();

const failed = (record: HostRecord): HostRecord => ({ ...record, error: "connection refused" });

const demo = host("http://a:7710", "demo", 1, [one, two], "held");
const writer = host("http://a:7711", "writer", 2, []);
const writerPeer = host("http://b:7710", "writer", 1, []);

const facts = (rows: ReturnType<typeof switcherRows>) =>
  rows.map(({ label, held, need, open, down }) => ({ label, held, need, open, down }));

test("the rows put All projects first, then pool each project's hosts in the order they first name it", () => {
  expect(facts(switcherRows([demo, writer, writerPeer], 0, NO_READINESS))).toEqual([
    { label: "All projects", held: false, need: 2, open: 4, down: false },
    { label: "demo", held: true, need: 2, open: 1, down: false },
    { label: "writer", held: false, need: 0, open: 3, down: false },
  ]);
});

test("a ci park and a pull_request park with no readiness need nothing, and still leave each open count", () => {
  const parked = [host("http://a:7710", "demo", 1, [ciPark]), host("http://a:7711", "writer", 2, [prPark])];
  expect(facts(switcherRows(parked, 0, NO_READINESS))).toEqual([
    { label: "All projects", held: false, need: 0, open: 3, down: false },
    { label: "demo", held: false, need: 0, open: 1, down: false },
    { label: "writer", held: false, need: 0, open: 2, down: false },
  ]);
});

test("a project is down only when every one of its hosts failed its last poll", () => {
  const partly = switcherRows([demo, failed(writer), writerPeer], 0, NO_READINESS);
  expect(partly.map((row) => row.down)).toEqual([false, false, false]);
  const wholly = switcherRows([demo, failed(writer), failed(writerPeer)], 0, NO_READINESS);
  expect(wholly.map((row) => row.down)).toEqual([false, false, true]);
  expect(wholly[2]!.need).toBe(2);
});

test("matching keeps the rows whose name holds the trimmed query, ignoring case", () => {
  const rows = switcherRows([demo, writer, writerPeer], 0, NO_READINESS);
  expect(matchRows(rows, " RIT ").map((row) => row.label)).toEqual(["writer"]);
  expect(matchRows(rows, "zz")).toEqual([]);
});
