import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { ShippedTable } from "../src/components/ShippedTable";
import { tagRows } from "../src/lib/shipped";
import type { ShippedBody, ShippedRow } from "../src/lib/types";
import { ShippedWithLedger } from "./ledger";
import { fixture, settle } from "./harness";

const HOST = { base: "http://writer:7710", project: "/srv/dev/writer" };
const threeDays = await fixture<{ now: number; shipped: ShippedBody }>("shipped_three_days.json");
const { now } = threeDays;
const ROWS = tagRows(HOST, threeDays.shipped.rows);

afterEach(cleanup);

const lastCell = (id: number) => {
  const row = document.querySelector(`[data-row='${id}']`)!;
  return row.children[row.children.length - 1]!;
};

test("the last header reads Change", () => {
  render(<ShippedTable rows={ROWS} now={now} tz="UTC" />);
  const headers = Array.from(document.querySelector("[data-day]")!.parentElement!.firstElementChild!.children);
  expect(headers[headers.length - 1]!.textContent).toBe("Change");
});

test("a row with pr_url ends in one PR #N link to the pull request titled with the merge sha and no sha text", () => {
  const url = "https://github.com/o/r/pull/2170";
  const row: ShippedRow = { ...ROWS[0]!, pr_url: url, commit_url: "https://github.com/o/r/commit/3f9c2ab0c1d2e3f4" };
  render(<ShippedTable rows={[row]} now={now} tz="UTC" />);
  const cell = lastCell(row.id);
  expect(cell.querySelectorAll("a").length).toBe(1);
  const link = cell.querySelector("a")!;
  expect(link.textContent).toBe("PR #2170");
  expect(link.getAttribute("href")).toBe(url);
  expect(link.getAttribute("title")).toBe("3f9c2ab0c1d2e3f4");
  expect(link.getAttribute("target")).toBe("_blank");
  expect(cell.textContent).toBe("PR #2170");
  expect(cell.querySelector("[data-sha]")).toBeNull();
});

test("a row without pr_url ends in the short sha linking to the commit", () => {
  const commit = "https://github.com/o/r/commit/3f9c2ab0c1d2e3f4";
  const row: ShippedRow = { ...ROWS[0]!, pr_url: null, commit_url: commit };
  render(<ShippedTable rows={[row]} now={now} tz="UTC" />);
  const cell = lastCell(row.id);
  expect(cell.querySelectorAll("a").length).toBe(1);
  const link = cell.querySelector("a")!;
  expect(link.textContent).toBe("3f9c2ab");
  expect(link.getAttribute("href")).toBe(commit);
  expect(cell.textContent).toBe("3f9c2ab");
  expect(cell.querySelector("[data-pr]")).toBeNull();
});


test("Finished defaults to Merged; All shows every outcome and polls and pages with outcome=all", async () => {
  const failed = { ...ROWS[0]!, id: 900, ticket: "KO-436", outcome: "failed", outcome_reason: "Verification failed\nFull diagnostic details", merge_sha: null };
  const merged = { ...ROWS[1]!, outcome: "merged" };
  const killed = { ...failed, id: 901, outcome: "killed", outcome_reason: "Stopped by operator" };
  const rejected = { ...failed, id: 902, outcome: "rejected", outcome_reason: "Pull request closed" };
  const asked: string[] = [];
  const deps = { fetch: async (url: string) => {
    asked.push(url);
    if (url.includes("/runs/")) return new Response("not found", { status: 404 });
    const all = new URL(url).searchParams.get("outcome") === "all";
    return Response.json({ rows: all ? [failed, killed, rejected, merged] : [merged], limit: 2, next_before: all ? merged.id : null });
  } };
  const view = render(<ShippedWithLedger hosts={[HOST]} now={now} deps={deps} />);
  await act(settle);
  expect(screen.getByRole("heading", { name: "Finished" })).toBeTruthy();
  expect(document.querySelector('[data-row="900"]')).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  await act(settle);
  const row = document.querySelector('[data-row="900"]') as HTMLElement;
  expect(within(row).getByText("failed")).toBeTruthy();
  expect(within(row).getByText("Verification failed")).toBeTruthy();
  expect(row.textContent).not.toContain("Full diagnostic details");
  expect(within(document.querySelector('[data-row="901"]') as HTMLElement).getByText("killed")).toBeTruthy();
  expect(within(document.querySelector('[data-row="902"]') as HTMLElement).getByText("rejected")).toBeTruthy();
  fireEvent.click(row);
  await act(settle);
  expect(row.parentElement!.querySelector("[data-detail]")).not.toBeNull();
  expect(asked).toContain(`${HOST.base}/runs/900`);
  fireEvent.click(row);
  expect(document.querySelector(`[data-row="${merged.id}"]`)).not.toBeNull();
  asked.length = 0;
  view.rerender(<ShippedWithLedger hosts={[HOST]} now={now} polls={1} deps={deps} />);
  await act(settle);
  expect(asked.length).toBeGreaterThan(0);
  expect(asked.every(url => new URL(url).searchParams.get("outcome") === "all")).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Load older" }));
  await act(settle);
  expect(asked.some(url => url.includes("before=") && url.includes("outcome=all"))).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Merged" }));
  await act(settle);
  expect(document.querySelector('[data-row="900"]')).toBeNull();
  expect(document.querySelector(`[data-row="${merged.id}"]`)).not.toBeNull();
});

test("All has a Finished empty state", async () => {
  const deps = { fetch: async () => Response.json({ rows: [], limit: 50, next_before: null }) };
  render(<ShippedWithLedger hosts={[HOST]} now={now} deps={deps} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "All" }));
  await act(settle);
  expect(screen.getByText("Nothing finished yet")).toBeTruthy();
});
