import { fakeFetch } from "./actionFakes";
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { AttentionRow } from "../src/components/AttentionRow";
import { RunDetail } from "../src/components/RunDetail";
import { ACTIONS_OFF } from "../src/lib/actions";
import { describe } from "../src/lib/attention";
import type { Fetch } from "../src/lib/poll";
import { storeToken } from "../src/lib/token";
import type { AttentionItem, RunDetailBody, Status } from "../src/lib/types";
import { fixture, settle } from "./harness";

const BASE = "http://writer:7710";
const TOKEN = "tok-612";
const PR = "https://github.com/o/r/pull/612";
const working = await fixture<Status>("working.json");
const live = working.runs[0]!;

/** `/runs/N` for the fixture's live run, parked on pull request `PR`. */
const DETAIL: RunDetailBody = {
  run: { id: live.id, ticket: live.ticket, phase: live.phase, started_ms: working.now - live.elapsed_ms,
    ended_ms: null, time_box_ms: live.time_box_ms, host: live.host, pr_url: PR },
  rounds: [],
  events: [],
};

const serving: Fetch = async (url) => {
  if (url.endsWith(`/runs/${live.id}`)) return Response.json(DETAIL);
  if (url.endsWith(`/runs/${live.id}/turns`)) return Response.json({ turns: [] });
  return new Response("not found", { status: 404 });
};

const STALE: AttentionItem = { kind: "stale_run", level: "attention", run: live.id, ticket: "KO-232",
  phase: "working", heartbeat_age_ms: 421_000, pr_url: null };

beforeEach(() => {
  localStorage.clear();
  storeToken("writer:7710", TOKEN);
});
afterEach(cleanup);

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

async function detail(actions: boolean, fetchImpl: Fetch) {
  render(<RunDetail base={BASE} id={live.id} now={working.now} polls={1} deps={{ fetch: serving }}
    daemon={{ base: BASE, actions, fetch: fetchImpl }} />);
  await act(settle);
}

test("Abort and close on a live run with a pull request posts {run, note, close: true} once a reason is typed", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "abort", ok: true, detail: `abort requested; run ${live.id} ends` });
  await detail(true, fetchImpl);
  await act(async () => { fireEvent.click(button("Abort and close")); });
  const box = screen.getByRole("textbox", { name: "Reason to abort and close" });
  expect(button("Send").disabled).toBe(true);
  fireEvent.change(box, { target: { value: "  " } });
  expect(button("Send").disabled).toBe(true);
  fireEvent.change(box, { target: { value: "wrong approach" } });
  await act(async () => { fireEvent.click(button("Send")); await settle(); });
  expect(seen).toEqual([{ url: `${BASE}/actions/abort`, method: "POST", authorization: `Bearer ${TOKEN}`,
    body: { run: live.id, close: true, note: "wrong approach" } }]);
});

test("a stale run without a pull request offers Abort alone, which posts close: false", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "abort", ok: true, detail: "done" });
  render(<ul><AttentionRow kind="stale_run" project="writer" runId={live.id}
    description={describe(STALE, working.thresholds, { now: working.now })}
    daemon={{ base: BASE, actions: true, fetch: fetchImpl }} /></ul>);
  expect(screen.queryByRole("button", { name: "Abort and close" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Kill run" })).toBeNull();
  await act(async () => { fireEvent.click(button("Abort")); });
  fireEvent.change(screen.getByRole("textbox", { name: "Reason to abort" }), { target: { value: "host gone" } });
  await act(async () => { fireEvent.click(button("Send")); await settle(); });
  expect(seen.map((request) => request.body)).toEqual([{ run: live.id, close: false, note: "host gone" }]);
});

test("a daemon without actions draws both aborts disabled, titled with the opt-in", async () => {
  const { seen, fetchImpl } = fakeFetch({ ok: true });
  await detail(false, fetchImpl);
  for (const name of ["Abort", "Abort and close"]) {
    expect(button(name).disabled).toBe(true);
    expect(button(name).getAttribute("title")).toBe(ACTIONS_OFF);
  }
  expect(seen).toEqual([]);
});
