import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, peersFetch, settle } from "../harness";

const DEMO = "http://writer-host:7710";
const working = (await Bun.file(new URL("../../../tests/fixtures/serve/status.json", import.meta.url)).json()) as Status;

const NOW = new Date(2026, 9, 15, 18, 0).getTime();
const THIRTY_DAYS_SINCE = String(new Date(2026, 8, 16).getTime());
const asked: Record<string, string>[] = [];

const polls = peersFetch(DEMO, {
  [DEMO]: { status: { ...working, project: "/srv/dev/demo", runs: [], queued: [] }, attention: { level: "none", now: NOW, project: "/srv/dev/demo", items: [] } },
});
const fetchImpl: Fetch = async (url) => {
  if (!url.includes("/history?")) return polls(url);
  asked.push(Object.fromEntries(new URL(url).searchParams));
  return Response.json({
    rows: [], since: Number(new URL(url).searchParams.get("since")), until: null, limit: 200, offset: 0, truncated: false, total: 0,
    counts: { state: { active: 0, merged: 0, failed: 0, abandoned: 0 }, failure: {}, implementer: {} }, implementers: [], now: NOW,
  });
};

async function mount(hash: string) {
  window.location.hash = hash;
  const fake = fakeDeps(fetchImpl);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 4; round += 1) await act(settle);
}

const hashPath = () => window.location.hash.split("?")[0];
const hashSearch = () => Object.fromEntries(new URLSearchParams(window.location.hash.split("?")[1]));

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
beforeEach(() => {
  asked.length = 0;
  localStorage.clear();
  sessionStorage.clear();
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});

test("an old history link opens Runs over the last 30 days, keeping its project and reading its outcome as the state", async () => {
  await mount("#/runs/history?project=demo&outcome=failed&page=3");
  expect(hashPath()).toBe("#/runs");
  expect(hashSearch()).toEqual({ project: "demo", state: "failed", range: "30d" });
  expect(screen.getByRole("list", { name: "Runs, newest first" })).toBeTruthy();
  expect(asked.at(-1)).toMatchObject({ since: THIRTY_DAYS_SINCE, state: "failed" });
});

test("a bare history link opens Runs over the last 30 days", async () => {
  await mount("#/runs/history");
  expect(hashPath()).toBe("#/runs");
  expect(hashSearch()).toEqual({ range: "30d" });
});

for (const outcome of ["nope", "active"]) {
  test(`an outcome the history page did not know, ${outcome}, opens Runs in no state`, async () => {
    await mount(`#/runs/history?outcome=${outcome}`);
    expect(hashPath()).toBe("#/runs");
    expect(hashSearch()).toEqual({ range: "30d" });
  });
}
