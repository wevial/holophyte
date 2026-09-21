import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { App } from "../src/App";
import { PullRequestTable } from "../src/components/PullRequestTable";
import { NeedsYou } from "../src/components/NeedsYou";
import { storeToken } from "../src/lib/token";
import type { AttentionItem, Status, RunDetailBody } from "../src/lib/types";
import { fakeFetch } from "./actionFakes";
import { fakeDeps, fixture, hostOf, settle } from "./harness";

const status = await fixture<Status>("idle.json");
const item: AttentionItem = {
  kind: "pr_open", level: "attention", run: 47, ticket: "KO-7",
  ticket_url: "https://linear.app/team/issue/KO-7", pr_url: "https://github.com/o/r/pull/2170",
  reason: "Waiting for maintainer review", asked_ms: status.now - 600000,
  pr: { checks: "success", review: "review_required", threads: 0 },
};
const host = hostOf({ ...status, project: "/projects/repo", actions: true }, { level: "attention", now: status.now, items: [item] });
afterEach(() => { cleanup(); localStorage.clear(); });

test("a parked candidate shows its project, linked ticket and PR, reason and band-format waiting time", () => {
  render(<PullRequestTable hosts={[host]} project="all" now={status.now} />);
  const row = screen.getAllByRole("row")[1]!;
  const cells = within(row).getAllByRole("cell");
  expect(screen.getByRole("heading", { name: "repo · 1" })).toBeTruthy();
  expect(within(cells[0]!).getByRole("link", { name: "KO-7" }).getAttribute("href")).toBe(item.ticket_url!);
  expect(within(cells[1]!).getByRole("link", { name: "#2170" }).getAttribute("href")).toBe(item.pr_url!);
  expect(cells[2]!.textContent).toContain("Waiting for maintainer review");
  expect(cells[2]!.textContent).toContain("checks green");
  expect(cells[2]!.textContent).toContain("review pending");
  expect(cells[3]!.textContent).toBe("10m");
});

test("project selection narrows both the table and the pointer, including a PR-only band", () => {
  const other = hostOf({ ...status, project: "/projects/other" }, { level: "attention", now: status.now,
    items: [{ ...item, ticket: "KO-8", run: 48 }] }, "http://writer:7711");
  const hosts = [host, other];
  const show = (project: string) => <><NeedsYou hosts={hosts} project={project} now={status.now} />
    <PullRequestTable hosts={hosts} project={project} now={status.now} /></>;
  const view = render(show("all"));
  expect(screen.getByRole("link", { name: "2 pull requests below" })).toBeTruthy();
  expect(screen.getAllByRole("row")).toHaveLength(4);
  view.rerender(show("/projects/repo"));
  expect(screen.getByRole("link", { name: "1 pull request below" })).toBeTruthy();
  expect(screen.getAllByRole("row")).toHaveLength(2);
  expect(screen.queryByText("KO-8")).toBeNull();
  expect(screen.getByText("Nothing needs you")).toBeTruthy();
  view.rerender(show("/projects/missing"));
  expect(screen.queryByRole("region", { name: "Pull requests" })).toBeNull();
  expect(screen.queryByText(/pull requests? below/)).toBeNull();
});

test("table actions retain Open PR and the private send-back note across polls", async () => {
  storeToken(host.address, "test-token");
  const { seen, fetchImpl } = fakeFetch({ ok: true, detail: "Sent back" });
  const opened: unknown[][] = [];
  const realOpen = window.open;
  window.open = ((...args: unknown[]) => { opened.push(args); return null; }) as typeof window.open;
  try {
    const view = render(<PullRequestTable hosts={[host]} project="all" now={status.now} actionFetch={fetchImpl} />);
    expect(screen.getAllByRole("button").map(button => button.textContent)).toEqual(["▸", "Open PR", "Send back with note"]);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Open PR" })); });
    expect(opened).toEqual([[item.pr_url, "_blank", "noopener,noreferrer"]]);
    expect(seen).toEqual([]);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send back with note" })); });
    expect((screen.getByRole("button", { name: "Send" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Please fix the heading" } });
    view.rerender(<PullRequestTable hosts={[{ ...host, attention: { ...host.attention!, items: [{ ...item }] } }]} project="all" now={status.now} actionFetch={fetchImpl} />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Please fix the heading");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Send" })); await settle(); });
    expect(seen).toEqual([{ url: `${host.base}/actions/send-back`, method: "POST", authorization: "Bearer test-token",
      body: { run: 47, note: "Please fix the heading" } }]);
    expect(screen.getByRole("status").textContent).toBe("Sent back");
    expect(screen.queryByRole("textbox")).toBeNull();
  } finally { window.open = realOpen; }
});

const detail: RunDetailBody = {
  run: { id: 47, ticket: "KO-7", phase: "pr_open", started_ms: status.now - 900000,
    ended_ms: null, time_box_ms: null, host: null },
  rounds: [
    { round: 9, started_ms: status.now - 300000, ended_ms: status.now - 200000, verdict: "revise",
      findings: [
        { path: "app.py", severity: "p1", message: "Preserve **validation**", verdict: "ADDRESS" },
        { path: "ui.ts", severity: "p2", message: "Keep the layout", verdict: "DECLINE" },
      ] },
    { round: 6, started_ms: status.now - 600000, ended_ms: status.now - 500000, verdict: "revise",
      findings: [{ path: "old.py", severity: "p2", message: "Old finding" }] },
  ],
  events: [
    { at: status.now - 120000, kind: "parked", summary: "Waiting for **review**" },
    { at: status.now - 700000, kind: "started", summary: "Started work" },
  ],
};

test("project tables follow Floor order, pool hosts, omit empty projects and the Project column", () => {
  const other = hostOf({ ...status, project: "/projects/alpha" }, { level: "attention", now: status.now,
    items: [{ ...item, ticket: "KO-8", asked_ms: status.now - 900000 }] }, "http://writer:7711");
  const empty = hostOf({ ...status, project: "/projects/empty" }, { level: "none", now: status.now, items: [] }, "http://writer:7712");
  const same = hostOf({ ...status, project: "/projects/repo" }, { level: "attention", now: status.now,
    items: [{ ...item, ticket: "KO-9" }] }, "http://writer:7713");
  render(<PullRequestTable hosts={[host, other, empty, same]} project="all" now={status.now} />);
  const tables = screen.getAllByRole("table");
  expect(tables).toHaveLength(2);
  expect(tables.map(table => table.getAttribute("aria-label"))).toEqual(["repo pull requests", "alpha pull requests"]);
  expect(screen.getByRole("heading", { name: "repo · 2" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "alpha · 1" })).toBeTruthy();
  expect(screen.queryByRole("columnheader", { name: "Project" })).toBeNull();
  expect(within(tables[0]!).getAllByRole("row")).toHaveLength(3);
  expect(screen.queryByText("empty")).toBeNull();
});

test("detail is lazy, shows the latest findings and full facts, and rows stay independently open across polls", async () => {
  const other = hostOf({ ...status, project: "/projects/repo" }, { level: "attention", now: status.now,
    items: [{ ...item, ticket: "KO-8" }] }, "http://writer:7711");
  const seen: string[] = [];
  const deps = { fetch: async (url: string) => { seen.push(url); return Response.json(detail); } };
  const show = (polls: number) => <StrictMode><PullRequestTable hosts={[structuredClone(host), structuredClone(other)]}
    project="all" now={status.now} polls={polls} deps={deps} /></StrictMode>;
  const view = render(show(0));
  await act(settle);
  expect(seen).toEqual([]);
  const toggle = screen.getByRole("button", { name: "Details for KO-7" });
  fireEvent.click(toggle);
  await act(settle);
  expect(seen).toEqual([`${host.base}/runs/47`]);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const row = toggle.closest("tr")!.nextElementSibling!;
  expect(row.id).toBe(toggle.getAttribute("aria-controls")!);
  expect(row.querySelector("td")!.colSpan).toBe(5);
  expect(Array.from(row.querySelectorAll("[data-verdict]"), el => el.textContent)).toEqual(["ADDRESS", "DECLINE"]);
  expect(row.textContent).not.toContain("Old finding");
  expect(row.querySelector("strong")!.textContent).toBe("validation");
  expect(row.textContent).toContain("Checks: success");
  expect(row.textContent).toContain("Review: review_required");
  expect(row.textContent).toContain("Open threads: 0");
  expect(row.textContent).toContain("Waiting for review");
  expect(row.textContent).toContain("2m ago");
  expect(within(row as HTMLElement).getByRole("link", { name: "Open run 47" }).getAttribute("href")).toBe(`${host.base}/#run=47`);
  const keyboardToggle = screen.getByRole("button", { name: "Details for KO-8" });
  keyboardToggle.focus();
  await act(async () => { await userEvent.keyboard("{Enter}"); });
  await act(settle);
  expect(seen).toEqual([`${host.base}/runs/47`, `${other.base}/runs/47`]);
  view.rerender(show(1));
  await act(settle);
  expect(screen.getAllByRole("button", { name: /Details for/, expanded: true })).toHaveLength(2);
  expect(seen).toHaveLength(4);
  fireEvent.click(toggle);
  expect(screen.getAllByRole("button", { name: /Details for/, expanded: true })).toHaveLength(1);
  expect(screen.getByRole("button", { name: "Details for KO-8" }).getAttribute("aria-expanded")).toBe("true");
});

test("pending and failed detail stay inside the detail row and retry recovers", async () => {
  let resolve!: (value: Response) => void;
  let calls = 0;
  const deps = { fetch: async () => { calls++; return calls === 1 ? new Promise<Response>(done => { resolve = done; }) : Response.json(detail); } };
  render(<PullRequestTable hosts={[host]} project="all" now={status.now} deps={deps} />);
  fireEvent.click(screen.getByRole("button", { name: "Details for KO-7" }));
  expect(screen.getByText("Loading run detail…").closest("tr")).toBe(screen.getAllByRole("row")[2]! as HTMLTableRowElement);
  await act(settle);
  await act(async () => { resolve(new Response("unavailable", { status: 503 })); await settle(); });
  expect(screen.getByRole("alert").closest("tr")).toBe(screen.getAllByRole("row")[2]! as HTMLTableRowElement);
  expect(screen.getByRole("button", { name: "Open PR" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  await act(settle);
  expect(calls).toBe(2);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelectorAll("[data-finding]")).toHaveLength(2);
});

test("the run link opens the existing run view on its serving daemon", async () => {
  window.location.hash = "#run=47";
  const seen: string[] = [];
  const { deps } = fakeDeps(async url => {
    seen.push(url);
    if (url.endsWith("/runs/47")) return Response.json(detail);
    if (url.endsWith("/status")) return Response.json(status);
    if (url.endsWith("/attention")) return Response.json({ level: "none", now: status.now, items: [] });
    return new Response("not found", { status: 404 });
  });
  try {
    render(<App base={host.base} pollDeps={deps} />);
    await act(settle);
    expect(screen.getByRole("region", { name: "Run 47" })).toBeTruthy();
    expect(screen.getByRole("article", { name: "run 47" })).toBeTruthy();
    expect(seen).toContain(`${host.base}/runs/47`);
    await act(async () => { window.location.hash = ""; window.dispatchEvent(new Event("hashchange")); });
    expect(screen.queryByRole("region", { name: "Run 47" })).toBeNull();
    expect(screen.getByRole("region", { name: "Floor" })).toBeTruthy();
  } finally { window.location.hash = ""; }
});
