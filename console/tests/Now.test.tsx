import { afterEach, expect, test } from "bun:test";
import { act, cleanup, render, screen } from "@testing-library/react";
import { Now } from "../src/components/Now";
import type { LedgerRow } from "../src/lib/ledger";
import type { Status } from "../src/lib/types";
import { fixture, hostOf, NO_ATTENTION, settle } from "./harness";

const status = await fixture<Status>("idle.json");
const now = new Date(2026, 8, 18, 15).getTime();
afterEach(cleanup);

async function show(versions: number[], ages: number[], clock = now) {
  const hosts = versions.map((schema_version, i) => hostOf(
    { ...status, host: "writer", project: `project-${i}`, schema_version },
    NO_ATTENTION, `http://writer:${7710 + i}`,
  ));
  const deps = { fetch: async (url: string) => {
    const i = hosts.findIndex((host) => url.startsWith(`${host.base}/`));
    const entries: LedgerRow[] = ages[i] === undefined ? [] : [{
      at: now - ages[i]!, action: "migrate", schema_to: versions[i], schema_from: versions[i]! - 1,
      project: `project-${i}`, run: null, ticket: null, kind: "intervention", source: "factory",
      text: `store schema ${versions[i]} (migration evidence)`,
    }];
    return Response.json({ entries, since: 0, limit: 1000 });
  } };
  const view = render(<Now hosts={hosts} project="all" now={clock} deps={deps} />);
  await act(settle);
  return { ...view, hosts, deps };
}

test("four project stores on one host render one schema summary", async () => {
  await show([24, 24, 24, 24], [120000, 90000, 60000, 0]);
  expect(screen.getAllByText(/stores at schema/)).toHaveLength(1);
  expect(screen.getByText(/writer · 4 stores at schema 24 \(migrated from 23/)).toBeTruthy();
});

test("an unmigrated older project is named alongside its migrated siblings", async () => {
  await show([24, 24, 24, 23], [120000, 60000, 0]);
  expect(screen.getByText(/3 stores at schema 24/)).toBeTruthy();
  expect(screen.getByText("writer · project-3 still at schema 23")).toBeTruthy();
});

test("migration evidence expires as the view clock advances past six hours", async () => {
  const view = await show([24], [0]);
  expect(screen.getByText(/store at schema 24/)).toBeTruthy();
  view.rerender(<Now hosts={view.hosts} project="all" now={now + 7 * 3600000} deps={view.deps} />);
  expect(screen.queryByText(/schema/)).toBeNull();
});
