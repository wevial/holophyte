import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { HostStatus, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, NO_ATTENTION, peersFetch, settle, type StubDaemon } from "../harness";

const ORIGIN = "http://writer:7710";
const SPARE = "http://spare:7710";
const at = (hour: number, minute: number) => new Date(2026, 9, 16, hour, minute).getTime();
const NOW = at(16, 53);
const fixtures = new URL("../../../tests/fixtures/serve/", import.meta.url);
const host = await Bun.file(new URL("host-status.json", fixtures)).json() as HostStatus;
const project = await Bun.file(new URL("status.json", fixtures)).json() as Status;

function fleet() {
  const writer: HostStatus = {
    ...host, now: NOW, daemon: { ...host.daemon, started_ms: NOW - 76 * 3_600_000 },
    build: { daemon: "9f3a1c2abcdef", head: "9f3a1c2abcdef", sweep: "9f3a1c2abcdef" },
    sweep: { ...host.sweep, ended: at(16, 50), state: "fresh" },
    projects: host.projects.map((entry, index) => ({
      ...entry, admission: index === 0 ? "enabled" : "held", runs: index === 0 ? entry.runs : [],
      supervisor: { ...entry.supervisor!, state: "live", heartbeat_age_ms: index === 0 ? 21_000 : 10_000 },
    })),
  };
  const spare: HostStatus = { ...host, now: at(16, 39), projects: [], sweep: { ...host.sweep, ended: at(16, 36) } };
  const daemons: Record<string, StubDaemon> = {
    [ORIGIN]: { status: writer, attention: NO_ATTENTION },
    [SPARE]: { status: spare, attention: NO_ATTENTION },
  };
  const roots = peersFetch(ORIGIN, daemons);
  const fetch: Fetch = async (url, init) => {
    const match = /^\/projects\/(alpha|beta)\/(status|attention)$/.exec(url.slice(ORIGIN.length));
    if (match) {
      if (match[2] === "attention") return Response.json({ ...NO_ATTENTION, project: `/root/${match[1]}` });
      const alpha = match[1] === "alpha";
      return Response.json({ ...project, project: `/root/${match[1]}`, workers: alpha ? 2 : 1, admission: alpha ? "enabled" : "held", runs: alpha ? project.runs.slice(0, 1) : [] });
    }
    return roots(url, init);
  };
  return { fetch, writer, fail: (base: string) => { daemons[base] = { down: new Error("connection refused") }; } };
}

async function mount(source = fleet(), hash = "#/settings/hosts", initialTime = at(16, 39)) {
  window.location.hash = hash;
  const fake = fakeDeps(source.fetch);
  fake.clock.now = initialTime;
  const client = consoleQueryClient();
  render(<QueryClientProvider client={client}><NextConsole base={ORIGIN} pollDeps={fake.deps} /></QueryClientProvider>);
  await act(settle);
  await act(settle);
  return {
    ...source,
    poll: async (now = NOW) => {
      fake.clock.now = now;
      await act(async () => { await client.refetchQueries({ queryKey: ["fleet", ORIGIN] }); });
      await act(settle);
      await act(settle);
    },
  };
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${ORIGIN}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
beforeEach(() => { localStorage.clear(); sessionStorage.clear(); });
afterEach(() => { cleanup(); window.location.hash = ""; localStorage.clear(); sessionStorage.clear(); });

test("host cards summarize the root projects and preserve a spare daemon's last report after its root fails", async () => {
  const view = await mount();
  view.fail(SPARE);
  await view.poll();
  expect(screen.getByText("2 daemons · 1 answering · polled 0s ago")).toBeTruthy();
  const cards = screen.getAllByRole("article");
  expect(cards.map((card) => within(card).getByRole("heading", { level: 2 }).textContent)).toEqual(["writer", "spare"]);
  const writer = within(screen.getByRole("article", { name: "writer" }));
  expect(writer.getByText("Healthy")).toBeTruthy();
  expect(writer.getByText("alpha").parentElement!.textContent).toContain("alpha taking work · 2 workers");
  expect(writer.getByText("beta").parentElement!.textContent).toContain("beta held · 1 worker");
  expect(writer.getByText("Running")).toBeTruthy();
  expect(writer.getByText("1 of 3 workers busy · beat 21s ago")).toBeTruthy();
  expect(writer.getByText("OK")).toBeTruthy();
  expect(writer.getByText("last 16:50")).toBeTruthy();
  expect(writer.getByText(/build 9f3a1c2/)).toBeTruthy();
  expect(writer.getByText(/up 3d 4h/)).toBeTruthy();
  expect(writer.queryByText(/checkout at/)).toBeNull();
  const spare = within(screen.getByRole("article", { name: "spare" }));
  expect(spare.getByText("Not answering · last seen 14m ago")).toBeTruthy();
  expect(spare.getByRole("status").textContent).toContain("spare has not answered since 16:39.");
  expect(spare.getByRole("status").textContent).toContain("it holds no projects, so no run is waiting on it");
  expect(spare.getByText("None. A spare host: it answers sweeps but claims nothing.")).toBeTruthy();
  expect(spare.getAllByText("Unknown")).toHaveLength(2);
  expect(spare.getByText(/last report 16:36: ok/)).toBeTruthy();
  expect(spare.getByText(/up unknown/)).toBeTruthy();
});

test("project read errors outrank a killed sweep in health while both and a different checkout remain visible", async () => {
  const source = fleet();
  source.writer.projects[1] = { ...source.writer.projects[1]!, error: "OperationalError: database is locked", supervisor: null, runs: [] };
  source.writer.sweep = { ...source.writer.sweep, state: "killed", exit: 1 };
  source.writer.build = { ...source.writer.build, head: "0a1b2c3abcdef" };
  await mount(source);
  const writer = within(screen.getByRole("article", { name: "writer" }));
  expect(writer.getByText("beta cannot be read")).toBeTruthy();
  expect(writer.getByText("OperationalError: database is locked")).toBeTruthy();
  expect(writer.queryByText("Not running") === null).toBe(true);
  expect(writer.getByText("Partly running")).toBeTruthy();
  expect(writer.getByText("Killed")).toBeTruthy();
  expect(writer.getByText(/last 16:50 · exit 1/)).toBeTruthy();
  expect(writer.getByText(/checkout at 0a1b2c3/)).toBeTruthy();
});

test("all failed roots retain both daemon cards and last-answer times even with a project search scope", async () => {
  const view = await mount(fleet(), "#/settings/hosts?project=alpha");
  view.fail(SPARE);
  await view.poll(at(16, 48));
  view.fail(ORIGIN);
  await view.poll();
  expect(screen.getByText("2 daemons · none answering · polled 0s ago")).toBeTruthy();
  const banner = screen.getByText("No host is answering this console.").closest('[role="status"]')!;
  expect(banner.textContent).toContain("last answer from writer at 16:48 · spare at 16:39");
  const writer = screen.getByRole("article", { name: "writer" });
  expect(banner.compareDocumentPosition(writer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(screen.getAllByRole("article")).toHaveLength(2);
  expect(screen.getByRole("article", { name: "spare" })).toBeTruthy();
  expect(within(writer).getAllByText("Unknown")).toHaveLength(2);
  expect(within(writer).getByText(/it holds alpha and beta; their runs are as of 16:48/)).toBeTruthy();
  expect(within(writer).getByText(/last report 16:48: running, 1 of 3 workers busy/)).toBeTruthy();
});

test("before the first poll the Hosts body asks without inventing a daemon card", async () => {
  const source = fleet();
  source.fetch = () => new Promise(() => {});
  await mount(source);
  expect(screen.getByText("asking…")).toBeTruthy();
  expect(screen.queryAllByRole("article")).toHaveLength(0);
});


test("daemon health and last-answer time follow the root when a project prefix asks for a token", async () => {
  const source = fleet();
  const fetch = source.fetch;
  let needsToken = false;
  source.fetch = (url, init) => needsToken && url === `${ORIGIN}/projects/alpha/status`
    ? Promise.resolve(new Response("token required", { status: 401 })) : fetch(url, init);
  const view = await mount(source);
  needsToken = true;
  await view.poll(at(16, 48));
  const answering = within(screen.getByRole("article", { name: "writer" }));
  const rootHealthy = answering.queryByText("Healthy") !== null;
  expect(screen.getByText("2 daemons · 2 answering · polled 0s ago")).toBeTruthy();
  view.fail(ORIGIN);
  await view.poll();
  const down = within(screen.getByRole("article", { name: "writer" }));
  expect({
    rootHealthy,
    lastSeen: down.queryByText("Not answering · last seen 5m ago") !== null,
    banner: down.getByRole("status").textContent?.includes("writer has not answered since 16:48."),
  }).toEqual({ rootHealthy: true, lastSeen: true, banner: true });
});
