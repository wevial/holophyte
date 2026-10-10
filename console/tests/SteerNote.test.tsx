import { fakeFetch } from "./actionFakes";
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RunDetail } from "../src/components/RunDetail";
import { ACTIONS_OFF } from "../src/lib/actions";
import type { Fetch } from "../src/lib/poll";
import { storeToken } from "../src/lib/token";
import type { RunDetailBody, Status } from "../src/lib/types";
import { fixture, settle } from "./harness";

const BASE = "http://writer:7710";
const TOKEN = "tok-steer";
const working = await fixture<Status>("working.json");
const live = working.runs[0]!;

const DETAIL: RunDetailBody = {
  run: { id: live.id, ticket: live.ticket, phase: live.phase, started_ms: working.now - live.elapsed_ms,
    ended_ms: null, time_box_ms: live.time_box_ms, host: live.host },
  rounds: [],
  events: [],
};

const serving: Fetch = async (url) => {
  if (url.endsWith(`/runs/${live.id}`)) return Response.json(DETAIL);
  if (url.endsWith(`/runs/${live.id}/turns`)) return Response.json({ turns: [] });
  return new Response("not found", { status: 404 });
};

beforeEach(() => {
  localStorage.clear();
  storeToken("writer:7710", TOKEN);
});
afterEach(cleanup);

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

async function liveCard(actions: boolean, fetchImpl: Fetch) {
  render(<RunDetail base={BASE} id={live.id} now={working.now} polls={1} deps={{ fetch: serving }}
    daemon={{ base: BASE, actions, fetch: fetchImpl }} />);
  await act(settle);
}

test("Steer on a live run posts its ticket, the note, a hint and a stop-now in one request and shows the detail", async () => {
  const detail = `${live.ticket}: steered run ${live.id}; its implementer turn stops now`;
  const { seen, fetchImpl } = fakeFetch({ action: "steer", ok: true, detail, recorded: 7 });
  await liveCard(true, fetchImpl);
  await act(async () => { fireEvent.click(button("Steer")); });
  fireEvent.change(screen.getByRole("textbox", { name: "Steer note" }), { target: { value: "keep the old flag" } });
  fireEvent.click(screen.getByRole("checkbox", { name: "Hint only" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Stop the turn now" }));
  await act(async () => { fireEvent.click(button("Send")); await settle(); });
  expect(seen).toEqual([{ url: `${BASE}/actions/steer`, method: "POST", authorization: `Bearer ${TOKEN}`,
    body: { ticket: live.ticket, hint: true, now: true, note: "keep the old flag" } }]);
  expect(screen.getByRole("status").textContent).toBe(detail);
});

test("a daemon without actions draws Steer disabled with ACTIONS_OFF, as Pause, and posts nothing", async () => {
  const { seen, fetchImpl } = fakeFetch({ ok: true, detail: "should not be asked" });
  await liveCard(false, fetchImpl);
  for (const name of ["Pause", "Steer"]) {
    expect(button(name).disabled).toBe(true);
    expect(button(name).getAttribute("title")).toBe(ACTIONS_OFF);
  }
  fireEvent.click(button("Steer"));
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(seen).toEqual([]);
});
