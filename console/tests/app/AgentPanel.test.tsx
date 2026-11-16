import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunAsks, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/app/fleet";
import { ConsoleApp } from "../../src/app/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, run, SERVE } from "./needsFleet";

const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const at = (minute: number) => new Date(2026, 9, 15, 16, minute).getTime();
const note = { id: 1, author: "maintainer", run: 12, consumed_by: null, consumed_ms: null, interrupt: false, withdrawn: false };
const detail: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 12, ticket: "DEMO-12", phase: "reviewing", ended_ms: null },
  steer_notes: [
    { ...note, id: 2, kind: "hint", note: "The lease tests cover this boundary.", at: at(50) },
    { ...note, kind: "amendment", note: "Keep the dry run read-only.", at: at(40), consumed_by: 12, consumed_ms: at(41) },
  ],
};
const emptyAsks: RunAsks = { run: 12, ticket: "DEMO-12", pr_url: null, asks: [] };

async function mount(body = detail, asks = emptyAsks) {
  const polls = fleet({ items: [], runs: body.run.ended_ms == null ? [{ ...run(12, "DEMO-12"), phase: "reviewing", round: 1 }] : [] }, { items: [] });
  const requests: string[] = [];
  const fetch: Fetch = async (url) => {
    requests.push(url);
    if (url === `${DEMO}/status`) {
      const status = (await (await polls(url)).json()) as Status;
      return Response.json({ ...status, route_labels: { ...status.route_labels, implementer: "claude opus" } });
    }
    if (url === `${DEMO}/runs/12`) return Response.json(body);
    if (url === `${DEMO}/runs/12/asks`) return Response.json(asks);
    return polls(url);
  };
  window.location.hash = "#/runs/demo/12";
  const fake = fakeDeps(fetch);
  fake.clock.now = NOW;
  render(
    <QueryClientProvider client={consoleQueryClient()}>
      <ConsoleApp base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return requests;
}

const panel = () => screen.getByRole("complementary", { name: "Agent" });
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

const rowOf = (text: string) => within(panel()).getByText(text).closest("li")!;
const meta = (row: Element) => [...row.querySelector("[data-meta]")!.children].map((part) => part.getAttribute("aria-label") ?? part.textContent);
const tooltip = () => screen.queryByRole("tooltip");
async function hover(mark: HTMLElement) {
  await act(async () => {
    fireEvent.keyDown(document.body, { key: "Tab" });
    mark.focus();
  });
  const words = tooltip()?.textContent;
  await act(async () => mark.blur());
  return words;
}

test("the reviewing run's head is a glyph named for its next round, Implementer and the label, with no caps label", async () => {
  const requests = await mount();
  const head = panel().querySelector("[data-agent-head]")!;
  expect(head.textContent).toBe("Implementerclaude opus");
  const glyph = within(head as HTMLElement).getByRole("img", { name: "idle until review round 2 returns" });
  expect(glyph.className).toContain("text-live");
  expect(await hover(glyph)).toBe("idle until review round 2 returns");
  expect(within(panel()).queryByText("Agent")).toBeNull();
  expect(panel().querySelector(".uppercase")).toBeNull();
  expect(requests.filter((url) => url === `${DEMO}/runs/12`)).toHaveLength(1);
  expect(requests.filter((url) => url === `${DEMO}/runs/12/asks`)).toHaveLength(1);
});

test("a read steer note and a waiting hint are plain rows in time order with a glyph meta and a read mark named on focus", async () => {
  await mount();
  const steer = rowOf("Keep the dry run read-only.");
  const hint = rowOf("The lease tests cover this boundary.");
  expect(steer.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  for (const row of [steer, hint]) expect(row.className).not.toMatch(/\bborder\b|\bbg-/);
  expect(meta(steer)).toEqual(["You", "steer", "16:40", "the agent read it at 16:41", "16:41"]);
  expect(meta(hint)).toEqual(["You", "hint", "16:50", "waiting until the agent reads it"]);
  expect(await hover(within(steer).getByRole("img", { name: "the agent read it at 16:41" }))).toBe("the agent read it at 16:41");
  expect(await hover(within(hint).getByRole("img", { name: "waiting until the agent reads it" }))).toBe("waiting until the agent reads it");
});

test("a note an earlier run of the chain read stays in the panel with its read time", async () => {
  await mount({ ...detail, steer_notes: [
    { ...note, id: 8, run: 11, kind: "amendment", note: "Keep the sweep idempotent.", at: at(0), consumed_by: 11, consumed_ms: at(5) },
  ] });
  expect(meta(rowOf("Keep the sweep idempotent."))).toEqual(["You", "steer", "16:00", "the agent read it at 16:05", "16:05"]);
});

test("a withdrawn note names its author and asks merge in time order with each answer in a reply row under its question", async () => {
  await mount({ ...detail, steer_notes: [
    { ...note, id: 3, kind: "amendment", author: "alex", note: "Use the existing lease fixture.", at: at(52), withdrawn: true, consumed_ms: at(53), consumed_by: 12 },
  ] }, { ...emptyAsks, asks: [
    { id: 4, author: "maintainer", question: "What does the dry run print?", asked_ms: at(53), answered_ms: at(55), answer: "One line per lease.", url: "https://github.com/example/project/pull/12#issuecomment-42" },
    { id: 5, author: "maintainer", question: "Can we reuse the fixture?", asked_ms: at(51), answered_ms: null, answer: null, url: null },
  ] });
  const rows = within(panel()).getAllByRole("listitem");
  expect(rows.map((row) => row.firstElementChild!.textContent)).toEqual([
    "Can we reuse the fixture?", "Use the existing lease fixture.", "What does the dry run print?",
  ]);
  expect(meta(rows[1]!)).toEqual(["alex", "steer", "16:52", "withdrawn"]);
  expect(within(rows[1]!).queryByText("16:53")).toBeNull();
  expect(meta(rows[0]!)).toEqual(["You", "ask", "16:51"]);
  const reply = rows[2]!.querySelector("[data-reply]")!;
  expect(reply.textContent).toBe("One line per lease.");
  expect(reply.querySelector("svg")).toBeTruthy();
  expect(meta(rows[2]!)).toEqual(["You", "ask", "16:53", "answer", "16:55"]);
  expect(rows[0]!.querySelector("[data-reply]")).toBeNull();
  expect(within(panel()).queryByRole("link", { name: /Open on GitHub/ })).toBeNull();
  expect(within(panel()).queryByText(/Open on GitHub/)).toBeNull();
});

const answered = (question: string, answer: string, url: string | null = null): RunAsks => ({ ...emptyAsks, asks: [
  { id: 6, author: "maintainer", question, asked_ms: at(51), answered_ms: at(54), answer, url },
] });

test("an answer renders its markdown as bold and code and links nowhere", async () => {
  const url = "https://github.com/example/project/pull/12#issuecomment-77";
  await mount({ ...detail, steer_notes: [] }, answered("When does the prompt show?",
    "**When the prompt shows.** In `components/x.tsx:104`, the modal opens once the lease is held.", url));
  const answer = panel().querySelector("[data-reply]")! as HTMLElement;
  expect(within(answer).getByText("When the prompt shows.").tagName).toBe("STRONG");
  expect(within(answer).getByText("components/x.tsx:104").tagName).toBe("CODE");
  expect(within(answer).queryAllByRole("link")).toHaveLength(0);
});

test("a maintainer ask written as a markdown list renders its items as a list", async () => {
  await mount({ ...detail, steer_notes: [] }, { ...emptyAsks, asks: [
    { id: 7, author: "maintainer", question: "Check these:\n\n- the lease sweep\n- the dry run", asked_ms: at(51), answered_ms: null, answer: null, url: null },
  ] });
  const ask = within(panel()).getAllByRole("listitem")[0]!.firstElementChild as HTMLElement;
  expect(within(ask).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["the lease sweep", "the dry run"]);
});

test("a raw script tag in an answer adds no script element to the document", async () => {
  const before = document.querySelectorAll("script").length;
  await mount({ ...detail, steer_notes: [] }, answered("Is it safe?", "Safe.\n\n<script>window.injected = true</script>"));
  expect(within(panel()).getByText("Safe.")).toBeTruthy();
  expect(document.querySelectorAll("script")).toHaveLength(before);
});

test("an ended run with no messages has a labelled Agent aside and explains that the agent stopped", async () => {
  await mount({ ...detail, run: { ...detail.run, phase: "done", outcome: "merged", ended_ms: at(56) }, steer_notes: [] });
  expect(panel().tagName).toBe("ASIDE");
  expect(panel().id).toBe("agent");
  expect(within(panel()).getByRole("img", { name: "stopped with the run" })).toBeTruthy();
  expect(within(panel()).getByText("Nothing sent to this run yet.")).toBeTruthy();
});
