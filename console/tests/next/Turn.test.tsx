import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { runTurnsSchema, type RunDetail } from "../../src/lib/contract.gen";
import type { Fetch } from "../../src/lib/poll";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const zone = process.env.TZ;
const servedRun = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const detail: RunDetail = {
  ...servedRun,
  run: { ...servedRun.run, id: 12, ticket: "DEMO-7", title: "The sweep frees a silent lease", host: "writer-host" },
};
const common = { route: "primary", session_id: null, ended_ms: Date.parse("2026-10-16T23:48:17Z"),
  exit_status: 0, timed_out: false, input_tokens: null, output_tokens: null, cost_usd: null };
const turns = runTurnsSchema.parse({ turns: [
  { ...common, id: 40, role: "implement", label: "claude opus", seconds: 372, cost_usd: 2.18 },
  { ...common, id: 41, role: "review", label: null, seconds: 100 },
  { ...common, id: 42, role: "implement", label: "claude opus", seconds: 185,
    input_tokens: 52100, output_tokens: 9300, cost_usd: 0.92 },
] });

const polls = fleet({ items: [] }, { items: [] });
const fetchTurn: Fetch = async url => {
  if (url === `${DEMO}/runs/12`) return Response.json(detail);
  if (url === `${DEMO}/runs/12/turns`) return Response.json(turns);
  return polls(url);
};

async function mount(hash: string, fetch: Fetch = fetchTurn) {
  window.location.hash = hash;
  const fake = fakeDeps(fetch);
  render(<QueryClientProvider client={consoleQueryClient()}>
    <NextConsole base={DEMO} pollDeps={fake.deps} />
  </QueryClientProvider>);
  for (let round = 0; round < 6; round += 1) await act(settle);
}

function figure(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  return within(term.parentElement!);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => {
  process.env.TZ = "America/Los_Angeles";
  happyDOM.setURL(`${DEMO}/next.html`);
});
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => {
  cleanup();
  window.location.hash = "";
});

test("a second implementer turn names its review return and displays recorded usage, elapsed time and cumulative cost", async () => {
  await mount("#/runs/demo/12/turns/42");
  const crumb = screen.getByRole("navigation", { name: "Breadcrumb" });
  expect(crumb.textContent).toBe("Runs / DEMO-7 / run 12 / turn 2");
  expect(within(crumb).getByRole("link", { name: "Runs" }).getAttribute("href")).toEndWith("#/runs");
  expect(within(crumb).getByRole("link", { name: "run 12" }).getAttribute("href")).toEndWith("#/runs/demo/12");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Implementer · turn 2");
  expect(screen.getByText("The sweep frees a silent lease · back from review round 1")).toBeTruthy();
  expect(figure("Seat").getByText("implementer")).toBeTruthy();
  expect(figure("Seat").getByText("writer-host")).toBeTruthy();
  expect(figure("Model").getByText("claude opus")).toBeTruthy();
  expect(figure("Model").getByText("primary route")).toBeTruthy();
  expect(figure("Duration").getByText("3m 05s")).toBeTruthy();
  expect(figure("Duration").getByText(/^16:45:12 – 16:48:17 /)).toBeTruthy();
  expect(figure("Tokens").getByText("61.4k")).toBeTruthy();
  expect(figure("Tokens").getByText("52.1k in · 9.3k out")).toBeTruthy();
  expect(figure("Cost").getByText("$0.92")).toBeTruthy();
  expect(figure("Cost").getByText("run so far $3.10")).toBeTruthy();
});

test("turn tabs and neighbouring links preserve run identity and mark the last turn current with next disabled", async () => {
  await mount("#/runs/demo/12/turns/42");
  const tabs = within(screen.getByRole("navigation", { name: "Turns of run 12" })).getAllByRole("link");
  for (const [index, title, duration] of [[0, "implementer · turn 1", "6m 12s"], [1, "reviewer · round 1", "1m 40s"], [2, "implementer · turn 2", "3m 05s"]] as const) {
    expect(within(tabs[index]!).getByText(title)).toBeTruthy();
    expect(within(tabs[index]!).getByText(duration)).toBeTruthy();
  }
  expect(tabs.map(tab => tab.getAttribute("aria-current"))).toEqual([null, null, "page"]);
  expect(tabs.map(tab => tab.getAttribute("href")?.split("#")[1])).toEqual([
    "/runs/demo/12/turns/40", "/runs/demo/12/turns/41", "/runs/demo/12/turns/42",
  ]);
  expect(screen.getByRole("link", { name: "Previous turn" }).getAttribute("href")).toEndWith("#/runs/demo/12/turns/41");
  expect(screen.getByLabelText("Next turn").getAttribute("aria-disabled")).toBe("true");
  expect(screen.getByRole("link", { name: "Back to the run" }).getAttribute("href")).toEndWith("#/runs/demo/12");
});

test("a review turn without usage or cost says not recorded and uses its review ordinal", async () => {
  await mount("#/runs/demo/12/turns/41");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Reviewer · round 1");
  expect(figure("Tokens").getByText("not recorded")).toBeTruthy();
  expect(figure("Cost").getByText("not recorded")).toBeTruthy();
  expect(figure("Model").getByText("not recorded")).toBeTruthy();
});

test.each([
  ["#/runs/demo/12/turns/99", "Run 12 has no turn 99.", "Back to the run"],
  ["#/runs/demo/99/turns/1", "demo has no run 99.", "Back to Runs"],
  ["#/runs/nope/12/turns/42", "No project named nope answers this console.", "Back to Runs"],
  ["#/runs/demo/invalid/turns/42", "demo has no run invalid.", "Back to Runs"],
  ["#/runs/demo/12/turns/invalid", "Run 12 has no turn invalid.", "Back to the run"],
])("%s identifies the missing resource and offers a way back", async (hash, message, back) => {
  await mount(hash);
  expect(screen.getByText(message)).toBeTruthy();
  expect(screen.getByRole("link", { name: back })).toBeTruthy();
});

test("a host still being asked exposes a loading status", async () => {
  await mount("#/runs/demo/12/turns/42", () => new Promise<Response>(() => {}));
  expect(within(screen.getByRole("main")).getByRole("status").textContent).toBe("Asking demo for run 12…");
});
