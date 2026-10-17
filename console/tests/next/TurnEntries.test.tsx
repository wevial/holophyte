import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { runTurnsSchema, transcriptSchema, type RunDetail, type Transcript } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, SERVE } from "./needsFleet";

const zone = process.env.TZ;
const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const detail = { ...served, run: { ...served.run, id: 12, host: "writer-host" } };
const turn = runTurnsSchema.parse({ turns: [{ id: 42, role: "implement", label: "claude opus", route: "primary",
  session_id: null, ended_ms: Date.parse("2026-10-16T23:48:17Z"), seconds: 185, exit_status: 0,
  timed_out: false, input_tokens: 52100, output_tokens: 9300, cost_usd: 0.92 }] }).turns[0]!;
const baseEntry = { tool: null, call: null, summary: null, failed: false, cut: false };
const transcript = transcriptSchema.parse({ entries: [
  { ...baseEntry, speaker: "user", text: "Implement the sweep fix.\nKeep the lease safe.", at_ms: Date.parse("2026-10-16T23:45:12Z") },
  { ...baseEntry, speaker: "assistant", text: "I will check the lease before changing it.", at_ms: Date.parse("2026-10-16T23:45:18Z") },
  { ...baseEntry, speaker: "error", text: "529 overloaded", at_ms: Date.parse("2026-10-16T23:46:24Z") },
  { ...baseEntry, speaker: "command", tool: "Bash", call: "bash-1", text: "python3 -m unittest discover -s tests -p 'test_sweep.py'", summary: "python3 -m unittest discover -s tests -p 'test_sweep.py'", at_ms: Date.parse("2026-10-16T23:46:32Z") },
  { ...baseEntry, speaker: "tool", tool: "Bash", call: "bash-1", text: "FAILED: lease remains held", failed: true, at_ms: Date.parse("2026-10-16T23:46:34.300Z") },
  { ...baseEntry, speaker: "command", tool: "Edit", call: "edit-1", text: "Replace stale lease release with guarded release.", summary: "Guard the lease release", at_ms: Date.parse("2026-10-16T23:47:00Z") },
  { ...baseEntry, speaker: "tool", tool: "Edit", call: "edit-1", text: "Updated sweep.py", at_ms: Date.parse("2026-10-16T23:47:01Z") },
] });

async function mount(answer: Transcript | number | "pending" = transcript, timedOut = false) {
  const urls: string[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fake = fakeDeps(async url => {
    urls.push(url);
    if (url === `${DEMO}/runs/12`) return Response.json(detail);
    if (url === `${DEMO}/runs/12/turns`) return Response.json({ turns: [{ ...turn, timed_out: timedOut }] });
    if (url === `${DEMO}/runs/12/turns/42/transcript`) {
      if (answer === "pending") return new Promise<Response>(() => {});
      return typeof answer === "number" ? new Response("unavailable", { status: answer }) : Response.json(answer);
    }
    return polls(url);
  });
  window.location.hash = "#/runs/demo/12/turns/42";
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let round = 0; round < 6; round += 1) await act(settle);
  return urls;
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => { process.env.TZ = "America/Los_Angeles"; happyDOM.setURL(`${DEMO}/next.html`); });
afterAll(() => {
  happyDOM.setURL("about:blank");
  if (zone == null) delete process.env.TZ;
  else process.env.TZ = zone;
});
afterEach(() => { cleanup(); window.location.hash = ""; });

test("the transcript pairs calls in chronological speaker rows, opens failures and ends with the recorded exit", async () => {
  const urls = await mount();
  const rows = within(screen.getByRole("list", { name: "Turn transcript" })).getAllByRole("listitem");
  expect(rows).toHaveLength(6);
  for (const [index, label, offset] of [[0, "Factory", "+0:00"], [1, "Assistant", "+0:06"], [2, "Error", "+1:12"], [3, "Tool call", "+1:20"], [4, "Tool call", "+1:48"], [5, "End", "+3:05"]] as const) {
    expect(within(rows[index]!).getByText(label)).toBeTruthy();
    expect(within(rows[index]!).getByText(offset)).toBeTruthy();
  }
  expect(within(rows[3]!).getByText("Bash")).toBeTruthy();
  expect(rows[3]!.querySelector("summary")!.textContent).toContain("python3 -m unittest discover -s tests -p 'test_sweep.py'");
  expect(rows[3]!.querySelector("summary")!.textContent).toContain("failed · 2.3s");
  expect(rows[3]!.querySelector("details")!.open).toBe(true);
  expect(rows[0]!.querySelector("details")!.open).toBe(false);
  expect(rows[4]!.querySelector("details")!.open).toBe(false);
  expect(within(rows[5]!).getByText("Turn ended · exit 0")).toBeTruthy();
  expect(rows.map(row => row.classList.contains("bg-bad-wash"))).toEqual([false, false, true, true, false, false]);
  const edit = rows[4]!.querySelector("details")!;
  await act(async () => { edit.open = true; fireEvent(edit, new Event("toggle")); });
  expect(within(edit).getByText("Input")).toBeTruthy();
  expect(within(edit).getByText("Replace stale lease release with guarded release.")).toBeTruthy();
  expect(within(edit).getByText("Output")).toBeTruthy();
  expect(within(edit).getByText("Updated sweep.py")).toBeTruthy();
  expect(urls.filter(url => url.endsWith("/transcript"))).toHaveLength(1);
});

test("an unmatched result keeps its output and truncation notice without inventing input", async () => {
  await mount(transcriptSchema.parse({ entries: [{ ...baseEntry, speaker: "tool", tool: "Read", call: "unmatched", text: "Partial file contents", at_ms: null, cut: true }] }));
  const row = within(screen.getByRole("list", { name: "Turn transcript" })).getAllByRole("listitem")[0]!;
  const fold = row.querySelector("details")!;
  await act(async () => { fold.open = true; fireEvent(fold, new Event("toggle")); });
  expect(within(row).getByText("Tool call")).toBeTruthy();
  expect(within(row).getByText("—")).toBeTruthy();
  expect(within(fold).queryByText("Input")).toBeNull();
  expect(within(fold).getByText("Output")).toBeTruthy();
  expect(fold.querySelector("pre")!.textContent).toStartWith("Partial file contents");
  expect(fold.querySelector("pre")!.textContent).toEndWith("cut at 20,000 characters");
});

test("a missing transcript preserves run figures and links to the run", async () => {
  await mount(404);
  expect(screen.getByText("writer-host has no transcript for this turn.")).toBeTruthy();
  expect(screen.getByText("The run's Progress log still has what this turn did; the figures above come from the run record.")).toBeTruthy();
  expect(screen.getByRole("link", { name: "Open the run" }).getAttribute("href")).toEndWith("#/runs/demo/12");
  const duration = screen.getByText("Duration", { selector: "dt" }).parentElement!;
  expect(within(duration).getByText("3m 05s")).toBeTruthy();
});

test("an empty timed-out turn explains why the agent wrote nothing", async () => {
  await mount(transcriptSchema.parse({ entries: [] }), true);
  expect(screen.getByText("This turn has no entries. It timed out before the agent wrote anything.")).toBeTruthy();
});

test("a host failure reports an alert instead of claiming the transcript is missing", async () => {
  await mount(500);
  expect(screen.getByRole("alert").textContent).toBe("Could not read the transcript from writer-host.");
});

test("a pending transcript announces fetching while the turn figures remain visible", async () => {
  await mount("pending");
  expect(within(screen.getByRole("main")).getByRole("status").textContent).toBe("Fetching the transcript from writer-host…");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Implementer · turn 1");
  const duration = screen.getByText("Duration", { selector: "dt" }).parentElement!;
  expect(within(duration).getByText("3m 05s")).toBeTruthy();
});

test("an untimed first user entry remains the folded prompt before a later note", async () => {
  await mount(transcriptSchema.parse({ entries: [
    { ...baseEntry, speaker: "user", text: "Original factory prompt", at_ms: null },
    { ...baseEntry, speaker: "user", text: "Later factory note", at_ms: Date.parse("2026-10-16T23:45:18Z") },
  ] }));
  const rows = within(screen.getByRole("list", { name: "Turn transcript" })).getAllByRole("listitem");
  expect(rows[0]!.querySelector("summary")?.textContent).toContain("Original factory prompt");
  expect(within(rows[0]!).getByText("—")).toBeTruthy();
  expect(rows[1]!.querySelector("details")).toBeNull();
  expect(within(rows[1]!).getByText("Later factory note")).toBeTruthy();
});
