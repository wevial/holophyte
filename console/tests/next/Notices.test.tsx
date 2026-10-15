import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ActiveOutage, HostStatus, LedgerEntry, Status } from "../../src/lib/contract.gen";
import { migrationLines } from "../../src/lib/migrations";
import type { Fetch } from "../../src/lib/poll";
import type { Status as PolledStatus } from "../../src/lib/types";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, hostOf, peersFetch, settle, type StubDaemon } from "../harness";

const DEMO = "http://writer-host:7710";
const SECOND = "http://writer-host-2:7711";
const SAME_HOST = "http://writer-host:7712";
const SERVE = new URL("../../../tests/fixtures/serve/", import.meta.url);
const working = (await Bun.file(new URL("status.json", SERVE)).json()) as Status;
const hostStatus = (await Bun.file(new URL("host-status.json", SERVE)).json()) as HostStatus;

const at = (hour: number, minute: number) => new Date(2026, 9, 15, hour, minute).getTime();
const MINUTE = 60_000;

const statusOf = (project: string, host = "writer-host", runs: Status["runs"] = []): Status => ({ ...working, project, host, runs, queued: [] });
const attentionOf = (now: number, project: string) => ({ level: "none", now, project, items: [] });
const projectDaemon = (project: string, now: number, host?: string, runs?: Status["runs"]): StubDaemon => ({
  status: statusOf(project, host, runs),
  attention: attentionOf(now, project),
});

async function mount(fetchImpl: Fetch, now: number) {
  window.location.hash = "#/";
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = now;
  const queries = consoleQueryClient();
  render(
    <QueryClientProvider client={queries}>
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return { queries, clock: fake.clock };
}

async function refetch(queries: ReturnType<typeof consoleQueryClient>) {
  await act(async () => {
    await queries.refetchQueries();
    await settle();
  });
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const strip = () => screen.queryByLabelText("Notices");
const banners = (kind: string) => Array.from(document.querySelectorAll(`[data-notice="${kind}"]`));
const footer = () => document.querySelector("[data-footer-line]")!;
const section = (name: string) => screen.getByRole("region", { name });
const SECTIONS = ["Needs you", "Running", "Pull requests", "Merged today"];

interface Project {
  name: string;
  status?: Status;
  failing?: boolean;
}

/** DEMO as a host daemon serving `projects`, its root answering `root`. */
function hostDaemon(root: Partial<HostStatus>, projects: Project[], now: number): Fetch {
  const listed = projects.map(({ name }) => ({ ...hostStatus.projects[0]!, name, path: `/srv/dev/${name}`, host: "writer-host" }));
  return async (url) => {
    if (url === `${DEMO}/peers`) return Response.json({ self: "writer-host:7710", peers: [] });
    if (url === `${DEMO}/status`) return Response.json({ ...hostStatus, now, ...root, projects: listed });
    if (url === `${DEMO}/attention`) return Response.json({ level: "none", now, items: [] });
    for (const project of projects) {
      const prefix = `${DEMO}/projects/${project.name}`;
      if (!url.startsWith(`${prefix}/`)) continue;
      if (project.failing) return new Response("boom", { status: 500 });
      if (url === `${prefix}/status`) return Response.json(project.status ?? statusOf(`/srv/dev/${project.name}`));
      if (url === `${prefix}/attention`) return Response.json(attentionOf(now, `/srv/dev/${project.name}`));
    }
    return new Response("not found", { status: 404 });
  };
}

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a stale console the page's own host daemon built opens the strip with its builds, reason and Reload", async () => {
  const NOW = at(12, 0);
  const stale = {
    build: { ...hostStatus.build, daemon: "9f3a1c2e7" },
    console: { ...hostStatus.console, stale: true, served: "0a1b2c3d4", reason: "the console tree changed after its build" },
  };
  await mount(hostDaemon(stale, [{ name: "demo" }], NOW), NOW);
  const [banner] = banners("stale-console");
  expect(strip()!.firstElementChild).toBe(banner!);
  const text = within(banner as HTMLElement);
  expect(text.getByText("This console is older than writer-host's daemon.")).toBeTruthy();
  expect(text.getByText("console 0a1b2c3 · daemon 9f3a1c2")).toBeTruthy();
  expect(text.getByText("the console tree changed after its build")).toBeTruthy();
  expect(text.getByRole("button", { name: "Reload" })).toBeTruthy();
  cleanup();

  await mount(hostDaemon({ ...stale, console: { ...stale.console, stale: false } }, [{ name: "demo" }], NOW), NOW);
  expect(banners("stale-console")).toHaveLength(0);
  expect(screen.queryByRole("button", { name: "Reload" })).toBeNull();
});

test("each route outage and today's migration in a host's ledger is one line of the strip", async () => {
  const NOW = at(15, 0);
  const outage: ActiveOutage = {
    project: 1, run: null, ticket: null, kind: "route_down", source: "loop", at: at(14, 2), reason: "probe exited 1", text: "implementer route down since 21:02 UTC",
  };
  const migrate: LedgerEntry = {
    at: at(13, 10), run: null, ticket: null, kind: "intervention", source: "operator", text: "migrated the store",
    action: "migrate", schema_from: 41, schema_to: 42, project: "demo",
  };
  const daemons = peersFetch(DEMO, { [DEMO]: projectDaemon("/srv/dev/demo", NOW) });
  const fetch: Fetch = async (url, init) => {
    if (url.startsWith(`${DEMO}/ledger?`)) return Response.json({ entries: [migrate], active_outages: [outage], since: at(0, 0), limit: 1000 });
    return daemons(url, init);
  };
  await mount(fetch, NOW);

  const host = hostOf(statusOf("/srv/dev/demo") as unknown as PolledStatus, { level: "none", now: NOW, items: [] }, DEMO, NOW);
  const migration = migrationLines([host], { [host.key]: { rows: [migrate] } }, NOW);
  expect(migration).toHaveLength(1);
  expect(migration[0]).toContain("at schema 42");
  const lines = within(strip()!).getAllByRole("listitem").map((line) => line.textContent);
  expect(lines).toEqual(["demo · implementer route down since 14:02 · probe exited 1", migration[0]!]);
});

test("a host that stopped answering gets a banner dated from its last answer, and its live run still counts", async () => {
  const run = { ...working.runs[0]!, id: 7, ticket: "WRITER-7", heartbeat_age_ms: 30_000 };
  const daemons: Record<string, StubDaemon> = {
    [DEMO]: projectDaemon("/srv/dev/demo", at(16, 48)),
    [SECOND]: projectDaemon("/srv/dev/writer", at(16, 48), "writer-host-2", [run]),
  };
  const { queries, clock } = await mount(peersFetch(DEMO, daemons), at(16, 48));
  expect(banners("host-silent")).toHaveLength(0);

  daemons[SECOND] = { down: new Error("connection refused") };
  clock.now = at(16, 48) + 4 * MINUTE;
  await refetch(queries);
  const [banner] = banners("host-silent");
  expect(banners("host-silent")).toHaveLength(1);
  expect(within(banner as HTMLElement).getByText("writer-host-2 is not answering.")).toBeTruthy();
  expect(within(banner as HTMLElement).getByText("last seen 4m ago · its rows below are from 16:48 and may be out of date")).toBeTruthy();
  expect(document.querySelector("[data-count-line]")!.textContent).toStartWith("1 running");
  cleanup();

  const silent = { [DEMO]: projectDaemon("/srv/dev/demo", at(16, 48)), [SECOND]: { down: new Error("connection refused") } };
  await mount(peersFetch(DEMO, silent), at(16, 48));
  const [never] = banners("host-silent");
  expect(within(never as HTMLElement).getByText("writer-host-2 is not answering.")).toBeTruthy();
  expect(within(never as HTMLElement).getByText("It has not answered since this page opened.")).toBeTruthy();
});

test("before any host /peers lists answers the sections show bare labels under an asking line, which goes at the first answer", async () => {
  const NOW = at(12, 0);
  const refused = { down: new Error("connection refused") };
  const daemons: Record<string, StubDaemon> = { [DEMO]: refused, [SAME_HOST]: refused };
  const { queries } = await mount(peersFetch(DEMO, daemons), NOW);

  const asking = () => screen.getAllByRole("status").filter((line) => line.textContent?.startsWith("Asking")).map((line) => line.textContent);
  expect(asking()).toEqual(["Asking writer-host for its projects…"]);
  for (const name of SECTIONS) {
    expect(section(name).querySelector("h2 > span")).toBeNull();
    expect(section(name).querySelector("p")).toBeNull();
  }
  expect(screen.queryByText("The factory has it from here.")).toBeNull();
  expect(screen.queryByText("No pull requests open.")).toBeNull();

  daemons[DEMO] = projectDaemon("/srv/dev/demo", NOW);
  daemons[SAME_HOST] = projectDaemon("/srv/dev/writer", NOW);
  await refetch(queries);
  expect(asking()).toEqual([]);
  expect(section("Needs you").querySelector("h2 > span")!.textContent).toBe("0");
  expect(screen.getByText("The factory has it from here.")).toBeTruthy();
  cleanup();

  await mount(peersFetch(DEMO, { [DEMO]: refused, [SECOND]: refused }), NOW);
  expect(asking()).toEqual(["Asking 2 hosts for their projects…"]);
});

test("the footer counts a host daemon's answering projects and ages its sweep, toning a stale one bad", async () => {
  const NOW = at(12, 0);
  const sweep = { ...hostStatus.sweep, state: "fresh" as const, ended: NOW - 2 * MINUTE };
  const three = [{ name: "alpha" }, { name: "beta" }, { name: "gamma" }];
  await mount(hostDaemon({ sweep }, three, NOW), NOW);
  expect(footer().textContent).toBe("writer-host · 3 projects answering · sweep ok 2m ago");
  cleanup();

  await mount(hostDaemon({ sweep }, [three[0]!, three[1]!, { name: "gamma", failing: true }], NOW), NOW);
  expect(footer().textContent).toBe("writer-host · 2 of 3 projects answering · sweep ok 2m ago");
  cleanup();

  await mount(hostDaemon({ sweep: { ...sweep, state: "stale" } }, three, NOW), NOW);
  const part = within(footer() as HTMLElement).getByText("sweep stale");
  expect(part.getAttribute("class")?.split(/\s+/)).toContain("text-bad");
  expect(footer().textContent).toBe("writer-host · 3 projects answering · sweep stale");
});
