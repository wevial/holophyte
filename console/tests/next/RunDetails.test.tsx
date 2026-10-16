import { afterAll, afterEach, beforeAll, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import type { RunDetail, RunFiles, RunTurns } from "../../src/lib/contract.gen";
import { consoleQueryClient } from "../../src/next/fleet";
import { NextConsole } from "../../src/next/router";
import { fakeDeps, settle } from "../harness";
import { DEMO, fleet, NOW, SERVE, WRITER } from "./needsFleet";

const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const detail: RunDetail = {
  ...served,
  run: { ...served.run, id: 12, ticket: "WRITER-12", phase: "reviewing", max_rounds: 3 },
  rounds: [
    {
      ...served.rounds[0]!, round: 1, reviewer_model: "codex gpt-6-sol",
      ended_ms: new Date(2026, 9, 15, 16, 45).getTime(),
      findings: [{ ...served.rounds[0]!.findings[0]!, severity: "p1", message: "Validate the diff base before reading files." }],
    },
    {
      ...served.rounds[0]!, round: 2, started_ms: new Date(2026, 9, 15, 16, 50).getTime(),
      ended_ms: null, verdict: "", findings: [],
    },
  ],
};
const files: RunFiles = {
  run: 12, base: "main", head: "task", truncated: false, total_added: 70, total_deleted: 10,
  files: [
    { path: "holophyte/serve/server.py", status: "M", added: 61, deleted: 9 },
    { path: "tests/test_server.py", status: "M", added: 7, deleted: 1 },
    { path: "docs/reference/http.md", status: "M", added: 1, deleted: 0 },
    { path: "README.md", status: "M", added: 1, deleted: 0 },
  ],
};
const turns: RunTurns = { turns: [
  { id: 3, role: "implement", label: "claude opus", route: "primary", seconds: null, session_id: null },
  { id: 1, role: "implement", label: "claude opus", route: "primary", seconds: 372, session_id: null },
  { id: 2, role: "review", label: "codex gpt-6-sol", route: "primary", seconds: 80, session_id: null },
] };

async function mount(fileAnswer: RunFiles | number = files, runDetail: RunDetail = detail) {
  const urls: string[] = [];
  const polls = fleet({ items: [] }, { items: [] });
  const fake = fakeDeps(async (url) => {
    urls.push(url);
    if (url === `${WRITER}/runs/12`) return Response.json(runDetail);
    if (url === `${WRITER}/runs/12/files`) {
      return typeof fileAnswer === "number" ? new Response("unavailable", { status: fileAnswer }) : Response.json(fileAnswer);
    }
    if (url === `${WRITER}/runs/12/turns`) return Response.json(turns);
    return polls(url);
  });
  fake.clock.now = NOW;
  window.location.hash = "#/runs/writer/12";
  render(<QueryClientProvider client={consoleQueryClient()}><NextConsole base={DEMO} pollDeps={fake.deps} /></QueryClientProvider>);
  for (let round = 0; round < 6; round += 1) await act(settle);
  return urls;
}

async function openDetails() {
  const fold = screen.getByText("Details", { exact: true }).closest("details")!;
  await act(async () => {
    fold.open = true;
    fireEvent(fold, new Event("toggle"));
    await settle();
  });
  await act(settle);
}

const happyDOM = (window as unknown as { happyDOM: { setURL: (url: string) => void } }).happyDOM;
beforeAll(() => happyDOM.setURL(`${DEMO}/next.html`));
afterAll(() => happyDOM.setURL("about:blank"));
afterEach(() => { cleanup(); window.location.hash = ""; });

test("Details loads files and turns from the run's host only after opening, and displays file counts", async () => {
  const urls = await mount();
  const fold = screen.getByText("Details", { exact: true }).closest("details")!;
  expect(fold.open).toBe(false);
  expect(urls.some((url) => /\/runs\/12\/(files|turns)$/.test(url))).toBe(false);
  await openDetails();
  expect(urls.filter((url) => /\/runs\/12\/(files|turns)$/.test(url)).sort()).toEqual([
    `${WRITER}/runs/12/files`, `${WRITER}/runs/12/turns`,
  ]);
  const changed = within(screen.getByRole("region", { name: "Files changed" }));
  expect(changed.getByRole("heading").textContent).toBe("Files changed · 4");
  expect(changed.getAllByRole("listitem")).toHaveLength(4);
  expect(changed.getByText("holophyte/serve/server.py").closest("li")!.textContent).toBe("holophyte/serve/server.py+61 −9");
});

test("Details shows completed review findings and the in-progress round's start and limit", async () => {
  await mount();
  await openDetails();
  const rounds = within(screen.getByRole("region", { name: "Review rounds" })).getAllByRole("listitem");
  expect(rounds[0]!.querySelector("div")!.textContent).toBe("Round 1: changes requested · codex gpt-6-sol · 16:45 · 1 finding");
  expect(rounds[0]!.querySelector("p")!.textContent).toBe("P1 Validate the diff base before reading files.");
  expect(rounds[1]!.textContent).toBe("Round 2: in progress · started 16:50 · max 3 rounds");
});

test("Transcript orders turns oldest first, counts each seat independently, and formats elapsed or running time", async () => {
  await mount();
  await openDetails();
  const transcript = within(screen.getByRole("region", { name: "Transcript" }));
  expect(transcript.getByRole("heading").textContent).toBe("Transcript · 3 turns");
  expect(transcript.getAllByRole("listitem").map((row) => [...row.children].map((cell) => cell.textContent))).toEqual([
    ["implementer · claude opus · turn 1", "6m 12s"],
    ["reviewer · codex gpt-6-sol · round 1", "1m 20s"],
    ["implementer · claude opus · turn 2", "running"],
  ]);
});

test.each([
  [409, "No branch or merge to diff."],
  [504, "git did not answer in time."],
] as const)("Details explains files endpoint status %i", async (status, message) => {
  await mount(status);
  await openDetails();
  expect(within(screen.getByRole("region", { name: "Files changed" })).getByRole("alert").textContent).toBe(message);
});

test("Details distinguishes an empty diff from an unavailable diff", async () => {
  await mount({ ...files, files: [], total_added: 0, total_deleted: 0 });
  await openDetails();
  const changed = within(screen.getByRole("region", { name: "Files changed" }));
  expect(changed.getByRole("heading").textContent).toBe("Files changed · 0");
  expect(changed.getByText("No files changed yet.")).toBeTruthy();
  expect(changed.queryByRole("alert")).toBeNull();
});

test("Details describes a successful completed review as passed", async () => {
  await mount(files, { ...detail, rounds: [{ ...detail.rounds[0]!, verdict: "pass", findings: [] }] });
  await openDetails();
  const round = within(screen.getByRole("region", { name: "Review rounds" })).getByRole("listitem");
  expect(round.textContent).toBe("Round 1: passed · codex gpt-6-sol · 16:45 · 0 findings");
});

test("Details labels a truncated file list as shown rather than a total", async () => {
  await mount({ ...files, truncated: true });
  await openDetails();
  const changed = within(screen.getByRole("region", { name: "Files changed" }));
  expect(changed.getByRole("heading").textContent).toBe("Files changed · 4 shown");
});
