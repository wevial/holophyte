import hostStatusFixture from "../../tests/fixtures/serve/host-status.json";
import hostAttentionFixture from "../../tests/fixtures/serve/host-attention.json";
import projectStatusFixture from "../../tests/fixtures/serve/status.json";
import sharedDetail from "../../tests/fixtures/serve/run-detail.json";
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { App } from "../src/App";
import { HostDaemonPanel } from "../src/components/HostDaemonPanel";
import { HostPanel } from "../src/components/HostPanel";
import { Hosts } from "../src/components/Hosts";
import { NeedsYou } from "../src/components/NeedsYou";
import { ProjectBlock } from "../src/components/ProjectBlock";
import { pollPeers } from "../src/hooks/usePeers";
import { describe } from "../src/lib/attention";
import { byDaemon, groupByHost, hostItems, mergeHosts, visibleHosts, type HostRecord } from "../src/lib/hosts";
import { tokenedFetch, type Fetch } from "../src/lib/poll";
import { runKey } from "../src/lib/runs";
import { hostStatusSchema, statusSchema } from "../src/lib/schemas";
import { storeToken } from "../src/lib/token";
import type { Attention, HostProject, HostStatus, Status } from "../src/lib/types";
import { NO_ATTENTION, fakeDeps, hostOf, settle } from "./harness";

const ORIGIN = "http://writer:7710";
const T = 1_750_000_000_000;
const host = hostStatusSchema.parse(hostStatusFixture);
const rootAttention = hostAttentionFixture as Attention;
const project = statusSchema.parse(projectStatusFixture);

/** Each project's own `/status` under its prefix: the project body, with
 *  run 1 in both stores (run ids are per store). */
const alphaStatus: Status = { ...project, project: "/root/alpha", actions: true, runs: [{ ...project.runs[0]!, id: 1, ticket: "KO-7" }] };
const betaStatus: Status = { ...project, project: "/root/beta", actions: true, runs: [{ ...project.runs[0]!, id: 1, ticket: "KO-9" }] };

type Project = { status: Status; attention: Attention } | { code: number; error: string };

/** A host daemon at `ORIGIN`: the root's `/status` and `/attention`, and
 *  each project's routes under `/projects/NAME`; every request is logged
 *  with the bearer it carried. */
function hostDaemon(root: HostStatus, projects: Record<string, Project>, attention: Attention = rootAttention) {
  const requests: { url: string; bearer: string | null }[] = [];
  const fetch: Fetch = async (url, init) => {
    requests.push({ url, bearer: new Headers(init?.headers).get("authorization") });
    const path = url.slice(ORIGIN.length);
    if (path === "/peers") return Response.json({ self: "writer:7710", peers: [] });
    if (path === "/status") return Response.json(root);
    if (path === "/attention") return Response.json(attention);
    const match = /^\/projects\/([^/]+)(\/.*)$/.exec(path);
    // The daemon decodes the segment before it asks the registry.
    const answer = match ? projects[decodeURIComponent(match[1]!)] : undefined;
    if (!match || !answer) return Response.json({ error: "not found" }, { status: 404 });
    if ("code" in answer) return Response.json({ error: answer.error }, { status: answer.code });
    if (match[2] === "/status") return Response.json(answer.status);
    if (match[2] === "/attention") return Response.json(answer.attention);
    const run = /^\/runs\/(\d+)$/.exec(match[2]!);
    if (run) {
      const ticket = answer.status.runs.find((candidate) => candidate.id === Number(run[1]))?.ticket ?? "KO-0";
      return Response.json({ ...sharedDetail, run: { ...sharedDetail.run, id: Number(run[1]), ticket } });
    }
    return Response.json({ error: "not found" }, { status: 404 });
  };
  return { fetch, requests };
}

const healthy = () =>
  hostDaemon(host, {
    alpha: { status: alphaStatus, attention: NO_ATTENTION },
    beta: { status: betaStatus, attention: NO_ATTENTION },
  });

async function pollOnceInto(previous: HostRecord[], fetch: Fetch, now = T): Promise<HostRecord[]> {
  return mergeHosts(previous, await pollPeers(ORIGIN, previous, { fetch }), now);
}

beforeEach(() => localStorage.clear());
afterEach(cleanup);

test("the host contract fixture parses as a host status and not as a project's", () => {
  expect<unknown>(hostStatusSchema.parse(hostStatusFixture)).toEqual(hostStatusFixture);
  expect(statusSchema.safeParse(hostStatusFixture).success).toBe(false);
  expect(hostStatusSchema.safeParse(projectStatusFixture).success).toBe(false);
});

test("a host daemon is a record per project at its prefix, one host group and one daemon, filtered by project", async () => {
  const { fetch, requests } = healthy();
  const hosts = await pollOnceInto([], fetch);
  expect(hosts.map((record) => [record.key, record.base, record.name, record.project])).toEqual([
    ["writer:7710/projects/alpha", `${ORIGIN}/projects/alpha`, "alpha", "/root/alpha"],
    ["writer:7710/projects/beta", `${ORIGIN}/projects/beta`, "beta", "/root/beta"],
  ]);
  expect(hosts.every((record) => record.address === "writer:7710" && record.host_status?.sweep.state === "fresh")).toBe(true);
  expect(requests.map((request) => request.url.slice(ORIGIN.length)).sort()).toEqual([
    "/attention", "/peers", "/projects/alpha/attention", "/projects/alpha/status",
    "/projects/beta/attention", "/projects/beta/status", "/status",
  ]);
  expect(visibleHosts(hosts, "/root/beta").map((record) => record.name)).toEqual(["beta"]);
  expect(visibleHosts(hosts, "all")).toHaveLength(2);
  // One card for the host, holding one daemon with both projects, beside a
  // project daemon on the same host that keeps its own panel.
  const other = { ...hostOf({ ...project, project: "/srv/dev/other" }, NO_ATTENTION, "http://writer:7711") };
  const groups = groupByHost([...hosts, other]);
  expect(groups.map((group) => [group.label, group.hosts.map((record) => record.key)])).toEqual([
    ["writer", ["writer:7710/projects/alpha", "writer:7710/projects/beta", "writer:7711"]],
  ]);
  expect(byDaemon(groups[0]!.hosts).map((records) => records.length)).toEqual([2, 1]);
});

test("run 1 in both projects is two runs: keyed and fetched under each project's prefix", async () => {
  const { fetch, requests } = healthy();
  const hosts = await pollOnceInto([], fetch);
  expect(runKey(hosts[0]!.base, 1)).not.toBe(runKey(hosts[1]!.base, 1));

  const { deps } = fakeDeps(fetch);
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  const floor = screen.getByRole("region", { name: "Floor" });
  const rows = [...floor.querySelectorAll<HTMLElement>('[data-run="1"]')];
  expect(rows).toHaveLength(2);
  requests.length = 0;
  fireEvent.click(within(rows[1]!).getAllByRole("button")[0]!);
  await act(settle);
  const details = requests.filter((request) => /\/runs\/1$/.test(request.url)).map((request) => request.url);
  expect(details).toEqual([`${ORIGIN}/projects/beta/runs/1`]);
  expect(screen.getByRole("article", { name: "run 1" }).textContent).toContain("KO-9");
});

test("one token per host: the bearer stored for the daemon's address rides every root and prefix request", async () => {
  storeToken("writer:7710", "machine-token");
  const { fetch, requests } = healthy();
  await pollOnceInto([], tokenedFetch(fetch));
  expect(requests.length).toBeGreaterThan(4);
  expect(new Set(requests.map((request) => request.bearer))).toEqual(new Set(["Bearer machine-token"]));
});

test("a project the root reports broken is that record's error alone and is not asked under its prefix", async () => {
  const broken: HostStatus = {
    ...host,
    projects: host.projects.map((entry) =>
      entry.name === "beta" ? { ...entry, error: "OperationalError: database is locked", supervisor: null, runs: [] } : entry),
  };
  const { fetch, requests } = hostDaemon(broken, { alpha: { status: alphaStatus, attention: NO_ATTENTION } });
  const hosts = await pollOnceInto([], fetch);
  const [alpha, beta] = hosts;
  expect(alpha).toMatchObject({ error: null, status: alphaStatus });
  expect(beta).toMatchObject({ key: "writer:7710/projects/beta", error: "OperationalError: database is locked", project: "/root/beta", status: null });
  expect(requests.some((request) => request.url.includes("/projects/beta/"))).toBe(false);
  const items = hostItems(beta!, T);
  expect(items).toHaveLength(1);
  expect(describe(items[0]!, { heartbeat_stale_ms: 0, strikes: 0 }, { now: T }).body).toBe("beta on writer is not answering");

  // A 503 under the prefix (a store locked after the root read it) keeps
  // the project's last good answer beside the error, the other untouched.
  const later = hostDaemon(host, {
    alpha: { status: alphaStatus, attention: NO_ATTENTION },
    beta: { code: 503, error: "OperationalError: database is locked" },
  });
  const good = await pollOnceInto([], healthy().fetch);
  const after = await pollOnceInto(good, later.fetch, T + 10_000);
  expect(after[0]).toMatchObject({ error: null, seen_ms: T + 10_000 });
  expect(after[1]).toMatchObject({ error: `${ORIGIN}/projects/beta/status answered 503`, status: betaStatus, seen_ms: T });
});

test("a host daemon whose root stops answering keeps every project record, each with the error", async () => {
  const good = await pollOnceInto([], healthy().fetch);
  const down: Fetch = async () => {
    throw new Error("connection refused");
  };
  const after = await pollOnceInto(good, down, T + 10_000);
  expect(after.map((record) => [record.key, record.error, record.root_failed, record.status?.project])).toEqual([
    ["writer:7710/projects/alpha", "connection refused", true, "/root/alpha"],
    ["writer:7710/projects/beta", "connection refused", true, "/root/beta"],
  ]);
});

test("a supervisor row on a host is the host sweep's: Run sweep posts to the daemon's root", async () => {
  const stale = { kind: "supervisor", level: "attention", state: "stale", heartbeat_age_ms: 300_000 };
  const lateSweep: Attention = {
    level: "attention",
    now: T,
    items: [{ kind: "sweep_stale", project: null, state: "killed", started: T - 200_000, ended: null, level: "attention" }],
  };
  const { fetch } = hostDaemon(
    host,
    { alpha: { status: alphaStatus, attention: { level: "attention", now: T, items: [stale] } }, beta: { status: betaStatus, attention: NO_ATTENTION } },
    lateSweep,
  );
  const hosts = await pollOnceInto([], fetch);
  const posts: string[] = [];
  const actionFetch: Fetch = async (url, init) => {
    posts.push(`${init?.method} ${url}`);
    return Response.json({ ok: true, detail: "started" });
  };
  render(<NeedsYou hosts={hosts} project="all" now={T} actionFetch={actionFetch} />);
  const band = screen.getByRole("region", { name: "Needs you" });
  expect(band.textContent).toContain("Host sweep is killed");
  expect(band.textContent).toContain("Host sweep last beat this store");
  const buttons = within(band).getAllByRole("button", { name: "Run sweep" });
  expect(buttons).toHaveLength(2);
  expect(within(band).queryByRole("button", { name: "Restart supervisor" })).toBeNull();
  fireEvent.click(buttons[0]!);
  await act(settle);
  expect(posts).toEqual([`POST ${ORIGIN}/actions/run-sweep`]);
});

test("the host card lists every project with its beat, runs and error, the last sweep and builds that differ", async () => {
  const mixed: HostStatus = {
    ...host,
    build: { daemon: "aaaaaaa1", sweep: "bbbbbbb2", head: "bbbbbbb2" },
    projects: host.projects.map((entry) =>
      entry.name === "beta"
        ? { ...entry, error: "SchemaNewer: schema newer than build", supervisor: null, runs: [] }
        : { ...entry, supervisor: { state: "live", pid: 0, heartbeat_age_ms: 20_000, host: null } }),
  };
  const { fetch } = hostDaemon(mixed, { alpha: { status: alphaStatus, attention: NO_ATTENTION } });
  const hosts = await pollOnceInto([], fetch);
  render(<HostDaemonPanel records={hosts} now={T} />);
  const card = screen.getByRole("article", { name: "writer" });
  expect(card.querySelector("[data-sweep]")!.textContent).toBe("fresh · ended 20s ago");
  expect(card.querySelector("[data-build-differ]")!.textContent).toBe("daemon aaaaaaa · sweep bbbbbbb · head bbbbbbb");
  const alpha = card.querySelector('[data-project-row="alpha"]')!;
  expect(alpha.querySelector("[data-beat]")!.textContent).toBe("live · host sweep · hb 20s");
  expect(alpha.querySelector("[data-project-runs]")!.textContent).toBe("1");
  expect(card.querySelector('[data-project-row="beta"] [data-project-error]')!.textContent).toBe("SchemaNewer: schema newer than build");
});

test("a project's own card names the host sweep for a pid-0 beat", () => {
  const swept = { ...alphaStatus, supervisor: { state: "live" as const, pid: 0, heartbeat_age_ms: 20_000, host: "writer" } };
  render(<HostPanel host={hostOf(swept, NO_ATTENTION)} now={T} />);
  expect(document.querySelector("[data-supervisor]")!.textContent).toBe("live · host sweep · hb 20s");
});

test("a linked run under a host daemon opens from its prefix; a path outside one is refused", async () => {
  const { fetch, requests } = healthy();
  const { deps } = fakeDeps(fetch);
  window.location.hash = `#run=1&daemon=${encodeURIComponent(`${ORIGIN}/projects/beta`)}`;
  render(<App base={ORIGIN} pollDeps={deps} />);
  await act(settle);
  expect(screen.getByRole("heading", { name: "Run 1" })).toBeTruthy();
  expect(requests.some((request) => request.url === `${ORIGIN}/projects/beta/runs/1`)).toBe(true);
  cleanup();
  window.location.hash = `#run=1&daemon=${encodeURIComponent(`${ORIGIN}/elsewhere`)}`;
  render(<App base={ORIGIN} pollDeps={fakeDeps(fetch).deps} />);
  await act(settle);
  expect(screen.queryByRole("heading", { name: "Run 1" })).toBeNull();
  window.location.hash = "";
});

/** `entry` as the root lists a project whose config gives no name. */
const unnamed = (entry: HostProject): HostProject => ({
  ...entry, name: null, store: null, project_row: null, supervisor: null, runs: [], schema_version: null, admission: null,
  error: `[holo2] ${entry.path}/.holophyte/config.toml: [serve] name must be a non-empty systemd instance name`,
});

test("a project whose config gives no name needs you beside a healthy one, and is never asked", async () => {
  const mixed: HostStatus = { ...host, projects: host.projects.map((entry) => (entry.name === "beta" ? unnamed(entry) : entry)) };
  const { fetch, requests } = hostDaemon(mixed, { alpha: { status: alphaStatus, attention: NO_ATTENTION } });
  const hosts = await pollOnceInto([], fetch);
  expect(hosts.map((record) => record.key)).toEqual(["writer:7710/projects/alpha"]);
  expect(requests.some((request) => request.url.includes("/projects/null"))).toBe(false);
  render(<NeedsYou hosts={hosts} project="all" now={T} />);
  const band = screen.getByRole("region", { name: "Needs you" });
  expect(band.textContent).toContain("/root/beta on writer is not answering");
  expect(band.textContent).toContain("[serve] name must be");
});

test("a host daemon with no named project keeps one card with its sweep and its broken entries", async () => {
  const none: HostStatus = { ...host, projects: host.projects.map(unnamed) };
  const hosts = await pollOnceInto([], hostDaemon(none, {}).fetch);
  expect(hosts).toHaveLength(1);
  expect(hosts[0]).toMatchObject({ key: "writer:7710", base: ORIGIN, name: null, error: null, status: null });
  expect(hostItems(hosts[0]!, T).map((item) => [item.kind, item.project])).toEqual([
    ["unreachable", "/root/alpha"],
    ["unreachable", "/root/beta"],
  ]);
  render(<Hosts hosts={hosts} project="all" now={T} />);
  const card = screen.getByRole("article", { name: "writer" });
  expect(card.querySelector("[data-sweep]")!.textContent).toBe("fresh · ended 20s ago");
  expect(card.querySelectorAll("[data-project-error]")).toHaveLength(2);
});

test("a name holding a space is asked percent-encoded under its prefix", async () => {
  const spaced: HostStatus = { ...host, projects: host.projects.map((entry) => (entry.name === "beta" ? { ...entry, name: "my project" } : entry)) };
  const { fetch, requests } = hostDaemon(spaced, {
    alpha: { status: alphaStatus, attention: NO_ATTENTION },
    "my project": { status: betaStatus, attention: NO_ATTENTION },
  });
  const hosts = await pollOnceInto([], fetch);
  expect(hosts[1]).toMatchObject({ name: "my project", base: `${ORIGIN}/projects/my%20project`, error: null, status: betaStatus });
  expect(requests.map((request) => request.url)).toContain(`${ORIGIN}/projects/my%20project/status`);
});

test("the Floor judges a host sweep's beat by the daemon's word, not the runs' threshold", () => {
  // A live pid-0 beat 45 s old against a 30 s run threshold: the daemon
  // judges it against two sweep intervals and calls it live.
  const swept: Status = {
    ...alphaStatus,
    thresholds: { ...alphaStatus.thresholds, heartbeat_stale_ms: 30_000 },
    supervisor: { state: "live", pid: 0, heartbeat_age_ms: 45_000, host: "writer" },
  };
  const group = (base: string) => ({ path: "/root/alpha", name: "alpha", base, status: swept, runs: [] });
  render(<ProjectBlock group={group(`${ORIGIN}/projects/alpha`)} expandedRun={null} onToggleRun={() => {}} />);
  expect(screen.getByRole("region", { name: "alpha" }).dataset.supervisor).toBe("live");
  cleanup();
  // A project daemon keeps the runs' threshold for its own supervisor.
  render(<ProjectBlock group={group(ORIGIN)} expandedRun={null} onToggleRun={() => {}} />);
  expect(screen.getByRole("region", { name: "alpha" }).dataset.supervisor).toBe("stale");
});
