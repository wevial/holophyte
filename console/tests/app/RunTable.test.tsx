import { afterEach, expect, test } from "bun:test";
import { act, cleanup, render } from "@testing-library/react";
import { createHashHistory, createRootRoute, createRouter, RouterProvider } from "@tanstack/react-router";
import type { Status } from "../../src/lib/contract.gen";
import type { HostRecord } from "../../src/lib/hosts";
import type { PeersState } from "../../src/hooks/usePeers";
import type { ContractBodies } from "../../src/app/fleet";
import type { RunRow } from "../../src/app/history";
import { RunTable } from "../../src/app/components/RunTable";
import { PeersContext } from "../../src/app/scope";
import { settle } from "../harness";

const HOST = "http://writer-host:7711";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const NOW = new Date(2026, 9, 15, 18, 0).getTime();

const row = (id: number, started_ms: number): RunRow => ({
  id, ticket: `DEMO-${id}`, title: "Count the rounds", started_ms, ticket_url: null, attempt: 1, phase: "reviewing",
  outcome: null, outcome_reason: null, failure_kind: null, park_kind: null, ended_ms: null, elapsed_ms: 60_000,
  heartbeat_age_ms: 1_000, rounds: 0, max_rounds: 3, working_ms: null, estimate_ms: null, pr_url: null, host: null,
  base: HOST, project: "demo", state: "running", seen_ms: null, thresholds: working.thresholds, stage: "reviewing",
});

afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("each day heading is filled with the hover tone and edged with the hairline, not the raised tone", async () => {
  const silent: HostRecord<ContractBodies> = {
    key: "writer-host:7711", address: "writer-host:7711", base: HOST, label: "writer-host:7711", project: null,
    status: null, attention: null, polled_ms: NOW, seen_ms: null, error: "connection refused", needs_token: false,
  };
  const peers: PeersState<ContractBodies> = { hosts: [silent], polledAgo: 0, polls: 1, now: NOW };
  const rows = [row(2, NOW - 60 * 60_000), row(1, NOW - 24 * 60 * 60_000)];
  const root = createRootRoute({
    component: () => (
      <PeersContext.Provider value={peers}>
        <RunTable rows={rows} now={NOW} empty="" grouped />
      </PeersContext.Provider>
    ),
  });
  window.location.hash = "#/";
  render(<RouterProvider router={createRouter({ routeTree: root, history: createHashHistory() })} />);
  await act(settle);
  const headings = Array.from(document.querySelectorAll("[data-day]"));
  expect(headings.map((heading) => heading.textContent)).toEqual(["Today 1", "Yesterday 1"]);
  for (const heading of headings) {
    expect(heading.classList).toContain("bg-hover");
    expect(heading.classList).toContain("shadow-[inset_0_0_0_1px_var(--hair)]");
    expect(heading.classList).not.toContain("bg-raised");
  }
});
