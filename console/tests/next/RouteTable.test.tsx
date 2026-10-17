import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { HostStatus, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, NO_ATTENTION, peersFetch, settle, type StubDaemon } from "../harness";

const ORIGIN = "http://writer:7710";
const at = (hour: number, minute: number) => new Date(2026, 9, 16, hour, minute).getTime();
const NOW = at(16, 39);
const fixtures = new URL("../../../tests/fixtures/serve/", import.meta.url);
const host = await Bun.file(new URL("host-status.json", fixtures)).json() as HostStatus;
const project = await Bun.file(new URL("status.json", fixtures)).json() as Status;

function status(name: string): Status {
  return {
    ...structuredClone(project), project: `/root/${name}`, now: NOW,
    route_labels: {
      ...project.route_labels, implementer: "claude opus", implementer_fallback: "claude sonnet",
      reviewer: "codex gpt-6-sol", reviewer_fallback: "claude opus",
    },
    active_routes: { ...project.active_routes, reviewer: { command: null, fallback: "claude" } },
    route_probes: {
      ...project.route_probes,
      implementer: { primary: { ok: true, at_ms: at(16, 38), age_ms: 41_000, duration_ms: 1800, why: null }, fallback: null },
      reviewer: { primary: { ok: false, at_ms: at(16, 36), age_ms: 180_000, duration_ms: 1000, why: "exit 1" }, fallback: null },
    },
  };
}

async function mount(projects: Status[] = [status("alpha")]) {
  const daemons: Record<string, StubDaemon> = {
    [ORIGIN]: {
      status: { ...host, now: NOW, projects: host.projects.filter((entry) => projects.some((body) => body.project === entry.path)) },
      attention: NO_ATTENTION,
    },
  };
  const roots = peersFetch(ORIGIN, daemons);
  const fetch: Fetch = async (url, init) => {
    const match = /^\/projects\/([^/]+)\/(status|attention)$/.exec(url.slice(ORIGIN.length));
    if (match) {
      const body = projects.find((entry) => entry.project === `/root/${match[1]}`);
      if (body) return Response.json(match[2] === "status" ? body : { ...NO_ATTENTION, project: body.project });
    }
    return roots(url, init);
  };
  window.location.hash = "#/settings/hosts";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  const client = consoleQueryClient();
  render(<QueryClientProvider client={client}><NextConsole base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return {
    fail: async () => {
      daemons[ORIGIN] = { down: new Error("connection refused") };
      fake.clock.now = at(16, 53);
      await act(async () => { await client.refetchQueries({ queryKey: ["fleet", ORIGIN] }); });
      await act(settle);
      await act(settle);
    },
  };
}

function row(seat: string) {
  const table = screen.getByRole("table", { name: "Agent routes on writer" });
  return within(within(table).getByRole("row", { name: new RegExp(`^${seat} `) }));
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("Hosts shows primary probes, recorded fallback use, route health and the fallback count", async () => {
  await mount();
  const implementer = row("implementer");
  expect(implementer.getByText("claude opus")).toBeTruthy();
  expect(implementer.getByText("ok · 41s ago · 1.8s")).toBeTruthy();
  expect(implementer.getByText("claude sonnet")).toBeTruthy();
  const reviewer = row("reviewer");
  expect(reviewer.getByText("codex gpt-6-sol")).toBeTruthy();
  expect(reviewer.getByText("down · failed 3m ago · exit 1")).toBeTruthy();
  expect(reviewer.getByText("claude opus")).toBeTruthy();
  expect(reviewer.getByText("in use · recorded")).toBeTruthy();
  expect(row("adjudicator").getByText("none")).toBeTruthy();
  expect(row("adjudicator").getByText("not probed yet")).toBeTruthy();
  expect(screen.getByText("1 route down · reviewer on its fallback")).toBeTruthy();
  expect(screen.getByText(/1 daemon · 1 answering · 1 route on a fallback/)).toBeTruthy();
  expect(screen.getByText(/routes are probed when the loop starts/)).toBeTruthy();
});

test("different project routes split a seat while matching routes share one row", async () => {
  const beta = status("beta");
  beta.route_labels.reviewer = "claude opus";
  await mount([status("alpha"), beta]);
  expect(row("reviewer · alpha").getByText("codex gpt-6-sol")).toBeTruthy();
  expect(row("reviewer · beta").getAllByText("claude opus")).toHaveLength(2);
  const table = within(screen.getByRole("table", { name: "Agent routes on writer" }));
  expect(table.getAllByRole("row", { name: /^implementer / })).toHaveLength(1);
  expect(table.queryByText("implementer · alpha")).toBeNull();
});

test("an unreachable root preserves the last route report and marks its probes unknown", async () => {
  const view = await mount();
  await view.fail();
  expect(screen.getByText("as of 16:39")).toBeTruthy();
  expect(row("implementer").getByText("unknown · ok at 16:38 · not probed since")).toBeTruthy();
  expect(row("reviewer").getByText("unknown · failed at 16:36")).toBeTruthy();
  expect(row("implementer").queryByText("ok · 41s ago · 1.8s")).toBeNull();
});

test("shared routes prefer a failed probe over a newer pass, otherwise the newest, and count fallback use per project", async () => {
  const beta = status("beta");
  beta.route_probes.implementer = {
    primary: { ok: true, at_ms: NOW - 5_000, age_ms: 5_000, duration_ms: 2400, why: null }, fallback: null,
  };
  beta.route_probes.reviewer = {
    primary: { ok: true, at_ms: NOW - 10_000, age_ms: 10_000, duration_ms: 1000, why: null }, fallback: null,
  };
  await mount([status("alpha"), beta]);
  expect(row("implementer").getByText("ok · 5s ago · 2.4s")).toBeTruthy();
  expect(row("reviewer").getByText("down · failed 3m ago · exit 1")).toBeTruthy();
  expect(screen.getByText(/2 routes on a fallback/)).toBeTruthy();
});

test("failed fallback probes and a seat without a fallback remain down without claiming fallback use", async () => {
  const alpha = status("alpha");
  alpha.route_labels.implementer_fallback = null;
  alpha.route_probes.implementer = {
    primary: { ok: false, at_ms: NOW - 30_000, age_ms: 30_000, duration_ms: 800, why: "unavailable" }, fallback: null,
  };
  alpha.active_routes.reviewer = { command: null, down: true };
  alpha.route_probes.reviewer!.fallback = {
    ok: false, at_ms: NOW - 179_000, age_ms: 179_000, duration_ms: 500, why: "429 rate limited",
  };
  await mount([alpha]);
  expect(row("reviewer").getByText("down · its fallback failed too")).toBeTruthy();
  expect(screen.getByText("2 routes down · implementer has no fallback")).toBeTruthy();
  expect(screen.queryByText("in use · recorded")).toBeNull();
  expect(screen.queryByText(/routes? on a fallback/)).toBeNull();
});
