import { afterAll, afterEach, beforeAll, beforeEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { ACTIONS_OFF } from "../../src/lib/actions";
import type { RunDetail, Status } from "../../src/lib/contract.gen";
import { addressOf } from "../../src/lib/hosts";
import type { Fetch } from "../../src/lib/poll";
import { storeToken } from "../../src/lib/token";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeFetch } from "../actionFakes";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const live: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 12, ticket: "DEMO-12", phase: "working", outcome: null, ended_ms: null },
  steer_notes: [],
};
const parked: RunDetail = {
  ...live,
  run: { ...live.run, id: 8, ticket: "DEMO-8", phase: "awaiting_merge_approval", pr_url: "https://github.com/example/project/pull/28" },
};

async function mount({ body = live, actions = true, answer }: { body?: RunDetail; actions?: boolean; answer?: Fetch } = {}) {
  const polls = fleet({ items: [], runs: body.run.ended_ms == null ? [{ ...run(body.run.id, body.run.ticket), phase: body.run.phase }] : [] }, { items: [] });
  const requests: string[] = [];
  const fake = fakeDeps(async (url, init) => {
    requests.push(url);
    if (url.includes("/actions/") && answer != null) return answer(url, init);
    if (url === `${DEMO}/status`) {
      const status = (await (await polls(url)).json()) as Status;
      return Response.json({ ...status, actions });
    }
    if (url === `${DEMO}/runs/${body.run.id}`) return Response.json(body);
    if (url === `${DEMO}/runs/${body.run.id}/asks`) return Response.json({ run: body.run.id, ticket: body.run.ticket, pr_url: body.run.pr_url, asks: [] });
    return polls(url);
  });
  fake.clock.now = NOW;
  window.location.hash = `#/runs/demo/${body.run.id}`;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return requests;
}

const agent = () => within(screen.getByRole("complementary", { name: "Agent" }));
const textbox = () => agent().getByRole("textbox") as HTMLTextAreaElement;
const toggle = (name: string) => agent().getByRole("button", { name });
const pressed = (name: string) => toggle(name).getAttribute("aria-pressed");
const toast = () => document.querySelector("[data-toast]")?.textContent ?? "";
async function click(element: HTMLElement) {
  await act(async () => void fireEvent.click(element));
  for (let round = 0; round < 4; round += 1) await act(settle);
}
async function type(value: string) {
  await act(async () => void fireEvent.change(textbox(), { target: { value } }));
}
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/`));
beforeEach(() => storeToken(addressOf(DEMO), "compose-token"));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("sending a live run's hint posts its ticket and flags, clears the draft and unpresses both toggles, toasts and refreshes both thread queries", async () => {
  const action = fakeFetch({ ok: true, detail: "steered" });
  const requests = await mount({ answer: action.fetchImpl });
  expect(within(agent().getByRole("group", { name: "Message kind" })).getByRole("button", { name: "Steer" }).getAttribute("aria-pressed")).toBe("true");
  expect(agent().getByRole("textbox", { name: "Note to the agent" }).getAttribute("placeholder")).toBe("Tell the agent what to change…");
  await type("Don't add a flag");
  await click(toggle("Just a hint"));
  const reads = (path: string) => requests.filter((url) => url === `${DEMO}${path}`).length;
  const before = [reads("/runs/12"), reads("/runs/12/asks")];
  await click(agent().getByRole("button", { name: "Send" }));
  expect(action.seen.map(({ url, body }) => ({ url, body }))).toEqual([
    { url: `${DEMO}/actions/steer`, body: { ticket: "DEMO-12", note: "Don't add a flag", hint: true, now: false } },
  ]);
  expect(toast()).toBe("Steered DEMO-12.");
  expect(textbox().value).toBe("");
  expect(pressed("Just a hint")).toBe("false");
  expect(pressed("Stop it now")).toBe("false");
  expect(reads("/runs/12")).toBeGreaterThan(before[0]!);
  expect(reads("/runs/12/asks")).toBeGreaterThan(before[1]!);
});

test("a live run's Steer and Ask are a sentence-case switch and the hint and stop toggles press and unpress", async () => {
  await mount();
  const kinds = within(agent().getByRole("group", { name: "Message kind" })).getAllByRole("button");
  expect(kinds.map((kind) => kind.textContent)).toEqual(["Steer", "Ask"]);
  for (const kind of kinds) expect(kind.className).not.toMatch(/\buppercase\b|\bborder\b/);
  expect(agent().getByRole("group", { name: "Message kind" }).className).not.toMatch(/\bborder\b/);
  for (const name of ["Just a hint", "Stop it now"]) {
    expect(pressed(name)).toBe("false");
    await click(toggle(name));
    expect(pressed(name)).toBe("true");
    await click(toggle(name));
    expect(pressed(name)).toBe("false");
  }
  expect(agent().queryByRole("checkbox")).toBeNull();
});

test("Stop it now turns Send into Stop and send, rejects blank notes, posts now and resets stop after success", async () => {
  const action = fakeFetch({ ok: true, detail: "stopped and steered" });
  await mount({ answer: action.fetchImpl });
  expect(agent().getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(true);
  await type("   ");
  expect(agent().getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(true);
  await click(toggle("Stop it now"));
  expect(agent().getByRole("button", { name: "Stop and send" }).hasAttribute("disabled")).toBe(true);
  await type("Use the existing option");
  await click(agent().getByRole("button", { name: "Stop and send" }));
  expect(action.seen[0]?.body).toEqual({ ticket: "DEMO-12", note: "Use the existing option", hint: false, now: true });
  expect(pressed("Stop it now")).toBe("false");
  expect(agent().getByRole("button", { name: "Send" }).hasAttribute("disabled")).toBe(true);
});

test("Ask on a parked pull request sends the run and question and preserves a refused question with the daemon detail", async () => {
  const detail = "console ask event 812 … is not answered yet";
  const action = fakeFetch({ ok: false, detail });
  await mount({ body: parked, answer: action.fetchImpl });
  expect(agent().queryByRole("button", { name: "Stop it now" })).toBeNull();
  await click(agent().getByRole("button", { name: "Ask" }));
  expect(agent().getByRole("textbox", { name: "Question for the agent" }).getAttribute("placeholder")).toBe("Ask about this run…");
  expect(agent().queryByRole("button", { name: "Just a hint" })).toBeNull();
  await type("Does it print?");
  await click(agent().getByRole("button", { name: "Send" }));
  expect(action.seen.map(({ url, body }) => ({ url, body }))).toEqual([
    { url: `${DEMO}/actions/ask`, body: { run: 8, question: "Does it print?" } },
  ]);
  expect(agent().getByRole("alert").textContent).toBe(detail);
  expect(textbox().value).toBe("Does it print?");
  expect(toast()).toBe("");
});

test("a successful Ask toasts that the answer appears here and clears the question", async () => {
  const action = fakeFetch({ ok: true, detail: "asked" });
  await mount({ body: parked, answer: action.fetchImpl });
  await click(agent().getByRole("button", { name: "Ask" }));
  await type("Does it print?");
  await click(agent().getByRole("button", { name: "Send" }));
  expect(toast()).toBe("Asked. The answer appears here.");
  expect(textbox().value).toBe("");
});

test("a live run disables Ask until the run parks on its pull request, and its header offers no link to the composer", async () => {
  await mount();
  const ask = agent().getByRole("button", { name: "Ask" });
  expect(ask.hasAttribute("disabled")).toBe(true);
  expect(ask.getAttribute("title")).toBe("Ask opens once the run parks on its pull request.");
  expect(screen.queryByRole("link", { name: "Steer or ask" })).toBeNull();
  expect(document.querySelector('a[href="#agent"]')).toBeNull();
  expect(agent().getByRole("textbox")).toBeTruthy();
});

test("actions off disables both message modes and Send with the host explanation", async () => {
  await mount({ actions: false });
  for (const name of ["Steer", "Ask", "Send"]) {
    const button = agent().getByRole("button", { name });
    expect(button.hasAttribute("disabled")).toBe(true);
    expect(button.getAttribute("title")).toBe(ACTIONS_OFF);
  }
});

test("a merged run keeps its notes under a stopped head glyph and holds no composer", async () => {
  const read = { id: 1, author: "maintainer", run: 12, kind: "amendment" as const, note: "Keep the dry run read-only.", at: NOW - 60_000, consumed_by: 12, consumed_ms: NOW - 30_000, interrupt: false, withdrawn: false };
  await mount({ body: { ...live, run: { ...live.run, phase: "done", outcome: "merged", ended_ms: NOW }, steer_notes: [read] } });
  expect(agent().getByText("Keep the dry run read-only.")).toBeTruthy();
  const glyph = agent().getByRole("img", { name: "stopped with the run" });
  expect(glyph.className).toContain("text-ink-3");
  expect(glyph.className).not.toContain("text-live");
  expect(glyph.querySelector("rect")).toBeTruthy();
  expect(glyph.querySelector(".state-pulse")).toBeNull();
  expect(agent().queryByRole("group", { name: "Message kind" })).toBeNull();
  expect(agent().queryByRole("textbox")).toBeNull();
});
