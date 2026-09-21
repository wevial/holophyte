import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { PullRequestTable } from "../src/components/PullRequestTable";
import { NeedsYou } from "../src/components/NeedsYou";
import { storeToken } from "../src/lib/token";
import type { AttentionItem, Status } from "../src/lib/types";
import { fakeFetch } from "./actionFakes";
import { fixture, hostOf, settle } from "./harness";

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
  expect(cells[0]!.textContent).toBe("repo");
  expect(within(cells[1]!).getByRole("link", { name: "KO-7" }).getAttribute("href")).toBe(item.ticket_url!);
  expect(within(cells[2]!).getByRole("link", { name: "#2170" }).getAttribute("href")).toBe(item.pr_url!);
  expect(cells[3]!.textContent).toContain("Waiting for maintainer review");
  expect(cells[3]!.textContent).toContain("checks green");
  expect(cells[3]!.textContent).toContain("review pending");
  expect(cells[4]!.textContent).toBe("10m");
});

test("project selection narrows both the table and the pointer, including a PR-only band", () => {
  const other = hostOf({ ...status, project: "/projects/other" }, { level: "attention", now: status.now,
    items: [{ ...item, ticket: "KO-8", run: 48 }] }, "http://writer:7711");
  const hosts = [host, other];
  const show = (project: string) => <><NeedsYou hosts={hosts} project={project} now={status.now} />
    <PullRequestTable hosts={hosts} project={project} now={status.now} /></>;
  const view = render(show("all"));
  expect(screen.getByRole("link", { name: "2 pull requests below" })).toBeTruthy();
  expect(screen.getAllByRole("row")).toHaveLength(3);
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
    expect(screen.getAllByRole("button").map(button => button.textContent)).toEqual(["Open PR", "Send back with note"]);
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
