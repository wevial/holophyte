import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";

const zone = process.env.TZ;
process.env.TZ = "America/Los_Angeles";

import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunAsks, RunDetail, Status } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
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
      <NextConsole base={DEMO} pollDeps={fake.deps} />
    </QueryClientProvider>,
  );
  for (let round = 0; round < 6; round += 1) await act(settle);
  return requests;
}

const panel = () => screen.getByRole("complementary", { name: "Agent" });
const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;

beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("the reviewing run names its implementer and next round and orders read and waiting notes from the shared page queries", async () => {
  const requests = await mount();
  const agent = within(panel());
  expect(agent.getByText("implementer · claude opus")).toBeTruthy();
  expect(agent.getByText("idle until review round 2 returns")).toBeTruthy();
  const steer = agent.getByText("You · steer · 16:40");
  const hint = agent.getByText("You · hint · 16:50");
  expect(steer.compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(agent.getByText("Keep the dry run read-only.")).toBeTruthy();
  expect(agent.getByText("→ the agent read it at 16:41")).toBeTruthy();
  expect(agent.getByText("The lease tests cover this boundary.")).toBeTruthy();
  expect(agent.getByText("→ waiting until the agent reads it")).toBeTruthy();
  expect(requests.filter((url) => url === `${DEMO}/runs/12`)).toHaveLength(1);
  expect(requests.filter((url) => url === `${DEMO}/runs/12/asks`)).toHaveLength(1);
});

test("the thread merges named authors and asks in time order and links the agent's separate answer to its posted comment", async () => {
  await mount({ ...detail, steer_notes: [
    { ...note, id: 3, kind: "amendment", author: "alex", note: "Use the existing lease fixture.", at: at(52), withdrawn: true, consumed_ms: at(53), consumed_by: 12 },
  ] }, { ...emptyAsks, asks: [
    { id: 4, author: "maintainer", question: "What does the dry run print?", asked_ms: at(53), answered_ms: at(55), answer: "One line per lease.", url: "https://github.com/example/project/pull/12#issuecomment-42" },
    { id: 5, author: "maintainer", question: "Can we reuse the fixture?", asked_ms: at(51), answered_ms: null, answer: null, url: null },
  ] });
  const agent = within(panel());
  const headers = ["You · ask · 16:51", "alex · steer · 16:52", "You · ask · 16:53", "Agent · 16:55"].map((text) => agent.getByText(text));
  for (let index = 1; index < headers.length; index += 1) {
    expect(headers[index - 1]!.compareDocumentPosition(headers[index]!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }
  expect(agent.getByText("Use the existing lease fixture.")).toBeTruthy();
  expect(agent.getByText("→ withdrawn")).toBeTruthy();
  expect(agent.queryByText("→ the agent read it at 16:53")).toBeNull();
  expect(agent.getByText("Can we reuse the fixture?")).toBeTruthy();
  expect(agent.getByText("What does the dry run print?")).toBeTruthy();
  expect(agent.getByRole("link", { name: "One line per lease." }).getAttribute("href")).toBe("https://github.com/example/project/pull/12#issuecomment-42");
});

test("an ended run with no messages has a labelled Agent aside and explains that the agent stopped", async () => {
  await mount({ ...detail, run: { ...detail.run, phase: "done", outcome: "merged", ended_ms: at(56) }, steer_notes: [] });
  expect(panel().tagName).toBe("ASIDE");
  expect(panel().id).toBe("agent");
  expect(document.getElementById(panel().getAttribute("aria-labelledby")!)?.textContent).toBe("Agent");
  expect(within(panel()).getByText("stopped with the run")).toBeTruthy();
  expect(within(panel()).getByText("Nothing sent to this run yet.")).toBeTruthy();
});
