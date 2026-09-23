import { fakeFetch } from "./actionFakes";
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { AttentionRow } from "../src/components/AttentionRow";
import { Now } from "../src/components/Now";
import { ProjectBlock } from "../src/components/ProjectBlock";
import { RunDetail } from "../src/components/RunDetail";
import { ACTIONS_OFF } from "../src/lib/actions";
import { describe } from "../src/lib/attention";
import type { Fetch } from "../src/lib/poll";
import { storeToken } from "../src/lib/token";
import type { AttentionItem, RunDetailBody, Status } from "../src/lib/types";
import { NO_ATTENTION, fixture, hostOf, settle } from "./harness";

const BASE = "http://writer:7710";
const TOKEN = "tok-610";
const working = await fixture<Status>("working.json");
const live = working.runs[0]!;

/** `/runs/91` for the fixture's live run: no round yet, still running. */
const DETAIL: RunDetailBody = {
  run: { id: live.id, ticket: live.ticket, phase: live.phase, started_ms: working.now - live.elapsed_ms,
    ended_ms: null, time_box_ms: live.time_box_ms, host: live.host },
  rounds: [],
  events: [],
};

/** A daemon serving `DETAIL` and nothing else. */
const serving: Fetch = async (url) => {
  if (url.endsWith(`/runs/${live.id}`)) return Response.json(DETAIL);
  if (url.endsWith(`/runs/${live.id}/turns`)) return Response.json({ turns: [] });
  return new Response("not found", { status: 404 });
};

const PAUSED: AttentionItem = { kind: "paused", level: "attention", ticket: "KO-232", run: live.id,
  note: "operator via the console: hold for the schema change", asked_ms: working.now - 60_000 };

beforeEach(() => {
  localStorage.clear();
  storeToken("writer:7710", TOKEN);
});
afterEach(cleanup);

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

function card(status: Status, actionFetch: Fetch) {
  const group = { path: status.project, name: "writer", base: BASE, status, runs: [] };
  render(<ProjectBlock group={group} expandedRun={null} onToggleRun={() => {}} actionFetch={actionFetch} />);
}

function pausedRow(actions: boolean, fetchImpl: Fetch) {
  render(<ul><AttentionRow kind="paused" project="writer" runId={live.id}
    description={describe(PAUSED, working.thresholds, { now: working.now })}
    daemon={{ base: BASE, actions, fetch: fetchImpl }} /></ul>);
}

/** Open `label`'s box, type `reason` and send it. */
async function pull(label: string, box: string, reason: string) {
  await act(async () => { fireEvent.click(button(label)); });
  fireEvent.change(screen.getByRole("textbox", { name: box }), { target: { value: reason } });
  await act(async () => { fireEvent.click(button("Send")); await settle(); });
}

test("Hold on an enabled project posts its reason with the bearer; Send waits for a non-blank reason", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "hold", ok: true, detail: "project /srv/dev/writer held" });
  card({ ...working, admission: "enabled", hold_note: null, actions: true }, fetchImpl);
  expect(screen.queryByRole("button", { name: "Release hold" })).toBeNull();
  await act(async () => { fireEvent.click(button("Hold")); });
  const box = screen.getByRole("textbox", { name: "Reason to hold" });
  expect(button("Send").disabled).toBe(true);
  fireEvent.change(box, { target: { value: "   " } });
  expect(button("Send").disabled).toBe(true);
  fireEvent.click(button("Send"));
  expect(seen).toEqual([]);
  fireEvent.change(box, { target: { value: "schema change lands first" } });
  await act(async () => { fireEvent.click(button("Send")); await settle(); });
  expect(seen).toEqual([{ url: `${BASE}/actions/hold`, method: "POST", authorization: `Bearer ${TOKEN}`,
    body: { note: "schema change lands first" } }]);
  expect(screen.getByRole("status").textContent).toBe("project /srv/dev/writer held");
});

test("a held project shows its hold note and Release hold, posting to /actions/release-hold", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "release-hold", ok: true, detail: "project /srv/dev/writer enabled" });
  card({ ...working, admission: "held", hold_note: "operator: schema change lands first", actions: true }, fetchImpl);
  expect(document.querySelector("[data-hold-note]")!.textContent).toBe("held: operator: schema change lands first");
  expect(screen.queryByRole("button", { name: "Hold" })).toBeNull();
  await pull("Release hold", "Reason to release hold", "schema change merged");
  expect(seen).toEqual([{ url: `${BASE}/actions/release-hold`, method: "POST", authorization: `Bearer ${TOKEN}`,
    body: { note: "schema change merged" } }]);
});

test("the Now view keeps an idle project's card, so Hold and Release hold outlive its runs", () => {
  const floor = (admission: string, holdNote: string | null) => {
    const status: Status = { ...working, admission, hold_note: holdNote, actions: true, runs: [] };
    render(<Now hosts={[hostOf(status, NO_ATTENTION, BASE)]} project="all" now={working.now} deps={{ fetch: serving }} />);
    const block = within(screen.getByRole("region", { name: "Floor" })).getByRole("region", { name: "writer" });
    const levers = within(block).getAllByRole("button").map((node) => node.textContent).filter((text) => text !== "Settings");
    const note = block.querySelector("[data-hold-note]")?.textContent ?? null;
    cleanup();
    return { levers, note };
  };
  expect(floor("enabled", null)).toEqual({ levers: ["Hold"], note: null });
  expect(floor("held", "operator: schema change lands first"))
    .toEqual({ levers: ["Release hold"], note: "held: operator: schema change lands first" });
});

test("Pause on a live run posts {run, note}", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "pause", ok: true, detail: `run ${live.id}: pause requested` });
  render(<RunDetail base={BASE} id={live.id} now={working.now} polls={1} deps={{ fetch: serving }}
    daemon={{ base: BASE, actions: true, fetch: fetchImpl }} />);
  await act(settle);
  await pull("Pause", "Reason to pause", "wrong base branch");
  expect(seen).toEqual([{ url: `${BASE}/actions/pause`, method: "POST", authorization: `Bearer ${TOKEN}`,
    body: { run: live.id, note: "wrong base branch" } }]);
  expect(screen.getByRole("status").textContent).toBe(`run ${live.id}: pause requested`);
});

test("the Now view offers Pause on an expanded live run, and not once a stop is requested", async () => {
  const footer = async (stopRequested: string | null) => {
    const status: Status = { ...working, actions: true, runs: [{ ...live, stop_requested: stopRequested }] };
    render(<Now hosts={[hostOf(status, NO_ATTENTION, BASE)]} project="all" now={working.now} deps={{ fetch: serving }} />);
    fireEvent.click(within(screen.getByRole("listitem")).getAllByRole("button")[0]!);
    await act(settle);
    const labels = Array.from(document.querySelectorAll("footer button")).map((node) => node.textContent);
    cleanup();
    return labels;
  };
  expect(await footer(null)).toEqual(["Abort", "Requeue ticket", "Pause"]);
  expect(await footer("operator via the console: wrong base branch")).toEqual(["Abort", "Requeue ticket"]);
});

test("a paused row reads as paused and Resume posts {ticket, note}, showing the daemon's detail", async () => {
  const { seen, fetchImpl } = fakeFetch({ action: "resume", ok: true, detail: "KO-232 resumed as run 92" });
  pausedRow(true, fetchImpl);
  expect(document.querySelector("span[data-kind]")!.textContent).toBe("paused");
  expect(screen.getByText(PAUSED.note as string)).toBeTruthy();
  await pull("Resume", "Reason to resume", "schema change merged");
  expect(seen).toEqual([{ url: `${BASE}/actions/resume`, method: "POST", authorization: `Bearer ${TOKEN}`,
    body: { ticket: "KO-232", note: "schema change merged" } }]);
  expect(screen.getByRole("status").textContent).toBe("KO-232 resumed as run 92");
});

test("a daemon without actions draws Hold, Pause and Resume disabled with ACTIONS_OFF and posts nothing", async () => {
  const { seen, fetchImpl } = fakeFetch({ ok: true, detail: "should not be asked" });
  card({ ...working, admission: "enabled", hold_note: null, actions: false }, fetchImpl);
  render(<RunDetail base={BASE} id={live.id} now={working.now} polls={1} deps={{ fetch: serving }}
    daemon={{ base: BASE, actions: false, fetch: fetchImpl }} />);
  await act(settle);
  pausedRow(false, fetchImpl);
  for (const name of ["Hold", "Pause", "Resume"]) {
    const lever = button(name);
    expect(lever.disabled).toBe(true);
    expect(lever.getAttribute("title")).toBe(ACTIONS_OFF);
    fireEvent.click(lever);
  }
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(seen).toEqual([]);
});
