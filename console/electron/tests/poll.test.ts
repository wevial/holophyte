import { expect, test } from "bun:test";

import hostStatus from "../../../tests/fixtures/serve/host-status.json";
import { pollAll } from "../poll.ts";
import { summarizeAnswer } from "../tray.ts";

const ORIGIN = "http://writer:7710";
const NOW = hostStatus.now;

/** Each project's own `/status` under its prefix, both with run 1. */
const projectStatus = (name: string, ticket: string) => ({
  project: `/root/${name}`,
  host: "writer",
  now: NOW,
  supervisor: { state: "live", pid: 0, heartbeat_age_ms: 5000 },
  thresholds: { heartbeat_stale_ms: 300000 },
  runs: [{ id: 1, ticket, phase: "working", heartbeat_age_ms: 12000 }],
});

/** A host daemon at the console's origin answering the host shape, with
 *  `beta` locked (the root lists its error); every request is logged with
 *  the bearer it carried. */
function hostDaemon(sweepState = "fresh") {
  const requests: { url: string; bearer: string | undefined }[] = [];
  const root = {
    ...hostStatus,
    sweep: { ...hostStatus.sweep, state: sweepState },
    projects: hostStatus.projects.map((project) =>
      project.name === "beta" ? { ...project, error: "OperationalError: database is locked" } : project),
  };
  const fetch = async (url: string, init: { headers: Record<string, string> }) => {
    requests.push({ url, bearer: init.headers.authorization });
    const path = url.slice(ORIGIN.length);
    if (path === "/peers") return Response.json({ self: "writer:7710", peers: [] });
    if (path === "/status") return Response.json(root);
    if (path === "/attention") return Response.json({ level: "none", now: NOW, items: [] });
    if (path === "/projects/alpha/status") return Response.json(projectStatus("alpha", "KO-7"));
    if (path === "/projects/alpha/attention") return Response.json({ level: "working", now: NOW, items: [] });
    return Response.json({ error: "not found" }, { status: 404 });
  };
  return { fetch, requests };
}

test("a host daemon's projects are an entry each under its prefix, asked with the one machine token", async () => {
  const { fetch, requests } = hostDaemon();
  const answer = await pollAll(`${ORIGIN}/`, { "writer:7710": "machine-token" }, { fetch, tokenFiles: { "writer:7710": "/seat/machine.token" } });
  expect(answer.peers).toEqual(["writer:7710/projects/alpha", "writer:7710/projects/beta"]);
  expect(Object.keys(answer.hosts)).toEqual(["writer:7710"]);
  expect(answer.statuses["writer:7710/projects/alpha"]).toMatchObject({ ok: true, body: { project: "/root/alpha" } });
  // The locked store is the root's word, and nothing is asked under its prefix.
  expect(answer.statuses["writer:7710/projects/beta"]).toMatchObject({ ok: false, kind: "http", status: 503 });
  expect(requests.some((request) => request.url.includes("/projects/beta/"))).toBe(false);
  expect(new Set(requests.filter((request) => !request.url.endsWith("/peers")).map((request) => request.bearer))).toEqual(
    new Set(["Bearer machine-token"]),
  );
  expect(answer.tokenFiles["writer:7710/projects/alpha"]).toBe("/seat/machine.token");

  const labels = summarizeAnswer(answer, NOW).items.filter((item) => item.type !== "separator").map((item) => item.label);
  expect(labels).toContain("alpha · working KO-7 · hb 12s");
  expect(labels).toContain("beta · OperationalError: database is locked");
  expect(labels).toContain("writer:7710 · sweep fresh · 20s ago");
  expect(labels).toContain("1 host · 1 daemon");
});

test("a host sweep that is not fresh is something that needs you", async () => {
  const { fetch } = hostDaemon("killed");
  const answer = await pollAll(`${ORIGIN}/`, {}, { fetch });
  const summary = summarizeAnswer(answer, NOW);
  expect(summary.items[0]?.label).toBe("writer:7710 · sweep killed · 20s ago");
  expect(summary.level).toBe("attention");
});
