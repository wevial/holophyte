import { afterEach, expect, test } from "bun:test";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { Shipped } from "../src/components/Shipped";
import { ShippedTable } from "../src/components/ShippedTable";
import { formatClock } from "../src/lib/format";
import type { Fetch } from "../src/lib/poll";
import { tagRows } from "../src/lib/shipped";
import type { ShippedBody, ShippedRow } from "../src/lib/types";
import { fixture, settle } from "./harness";

const BASE = "http://writer:7710";
const HOST = { base: BASE, project: "/srv/dev/writer" };
const threeDays = await fixture<{ now: number; shipped: ShippedBody }>("shipped_three_days.json");
const { now } = threeDays;
const ROWS = tagRows(HOST, threeDays.shipped.rows);

afterEach(cleanup);

/** A `/shipped` answering `pages` by their `before` (the first page has none)
 *  and recording every url asked. */
function pagedFetch(pages: Record<string, ShippedBody>) {
  const requests: string[] = [];
  const fetchImpl: Fetch = async (url) => {
    requests.push(url);
    const before = new URL(url).searchParams.get("before") ?? "";
    const page = pages[before];
    return page ? Response.json(page) : new Response("not found", { status: 404 });
  };
  return { fetchImpl, requests };
}

const dayHeaders = () =>
  Array.from(document.querySelectorAll("[data-day]")).map((group) => ({
    label: group.querySelector("[data-day-header] span")!.textContent,
    count: group.querySelector("[data-day-header] span + span")!.textContent,
    rows: Array.from(group.querySelectorAll("[data-row]")).map((row) => Number(row.getAttribute("data-row"))),
  }));

test("the table renders three day sub-headers with their merge counts and the rows newest-first beneath", () => {
  render(<ShippedTable rows={ROWS} now={now} tz="UTC" />);
  expect(dayHeaders()).toEqual([
    { label: "Today · Sat Sep 5", count: "3 merges", rows: [236, 235, 234] },
    { label: "Yesterday · Fri Sep 4", count: "2 merges", rows: [228, 227] },
    { label: "Thu Sep 3", count: "2 merges", rows: [224, 223] },
  ]);
  const first = document.querySelector("[data-row='236']")!;
  expect(first.textContent).toContain(formatClock(ROWS[0]!.ended_ms));
  expect(within(first as HTMLElement).getByText("KO-236")).toBeTruthy();
  expect(within(first as HTMLElement).getByText("writer")).toBeTruthy();
  expect(first.querySelector("[data-sha]")!.textContent).toBe("3f9c2ab");
  expect(first.querySelector("[data-sha]")!.className).toContain("text-link");
});

test("the Project cell names the daemon's project, and the host label appears nowhere in the row", () => {
  const row: ShippedRow = { ...ROWS[0]!, host: "writer-1", project: "holophyte" };
  render(<ShippedTable rows={[row]} now={now} tz="UTC" />);
  const cells = Array.from(document.querySelector("[data-row='236']")!.children).map((cell) => cell.textContent);
  expect(cells[3]).toBe("holophyte");
  expect(cells.some((text) => text!.includes("writer-1"))).toBe(false);
});

test("a row with commit_url renders its short sha as a new-tab anchor to the commit on origin", () => {
  const row: ShippedRow = { ...ROWS[0]!, commit_url: "https://github.com/example/writer/commit/3f9c2ab0c1d2e3f4" };
  render(<ShippedTable rows={[row]} now={now} tz="UTC" />);
  const sha = document.querySelector("[data-row='236'] [data-sha]") as HTMLAnchorElement;
  expect(sha.tagName).toBe("A");
  expect(sha.getAttribute("href")).toBe("https://github.com/example/writer/commit/3f9c2ab0c1d2e3f4");
  expect(sha.getAttribute("target")).toBe("_blank");
  expect(sha.getAttribute("rel")).toBe("noopener noreferrer");
  expect(sha.textContent).toBe("3f9c2ab");
  expect(sha.className).toContain("text-link");
  expect(sha.className).toContain("hover:underline");
});

test("a row with commit_url null renders the plain short sha with no anchor", () => {
  const row: ShippedRow = { ...ROWS[0]!, commit_url: null };
  render(<ShippedTable rows={[row]} now={now} tz="UTC" />);
  const sha = document.querySelector("[data-row='236'] [data-sha]")!;
  expect(sha.tagName).toBe("SPAN");
  expect(sha.textContent).toBe("3f9c2ab");
  expect(sha.className).toContain("text-link");
  expect(document.querySelector("[data-row='236'] a")).toBeNull();
});

test("days=1 keeps only today's group", () => {
  render(<ShippedTable rows={ROWS} now={now} tz="UTC" days={1} />);
  expect(dayHeaders().map((group) => group.label)).toEqual(["Today · Sat Sep 5"]);
});

test("the bar's fill and delta take the ok, warn and over tones at 24/30, 25/30, 30/30 and 38/30", () => {
  const at = (actual: number): ShippedRow => ({ ...ROWS[0]!, id: actual, actual_min: actual, estimate_min: 30 });
  render(<ShippedTable rows={[24, 25, 30, 38].map(at)} now={now} tz="UTC" />);
  const bars = screen.getAllByRole("progressbar");
  expect(bars.map((bar) => bar.getAttribute("data-tone"))).toEqual(["ok", "warn", "warn", "over"]);
  expect(bars.map((bar) => bar.firstElementChild!.className)).toEqual(["bg-ok", "bg-warn", "bg-warn", "bg-bad"].map((c) => `block h-full ${c}`));
  expect(bars.map((bar) => (bar.firstElementChild as HTMLElement).style.width)).toEqual(["80%", `${(25 / 30) * 100}%`, "100%", "100%"]);
  const deltas = Array.from(document.querySelectorAll("[data-delta]"));
  expect(deltas.map((delta) => delta.textContent)).toEqual(["−6m", "−5m", "0m", "+8m"]);
  expect(deltas.map((delta) => delta.getAttribute("data-delta"))).toEqual(["ok", "ok", "muted", "over"]);
  expect(deltas[0]!.className).toContain("text-ok-text");
  expect(deltas[3]!.className).toContain("text-bad-text");
  expect(Array.from(document.querySelectorAll("[data-minutes]")).map((m) => m.textContent)).toEqual([
    "24m / 30m",
    "25m / 30m",
    "30m / 30m",
    "38m / 30m",
  ]);
});

test("a row without a box has an empty bar, Nm / — and no delta", () => {
  render(<ShippedTable rows={[ROWS[6]!]} now={now} tz="UTC" />);
  const bar = screen.getByRole("progressbar");
  expect(bar.getAttribute("data-tone")).toBe("none");
  expect(bar.firstElementChild).toBeNull();
  expect(document.querySelector("[data-minutes]")!.textContent).toBe("15m / —");
  expect(document.querySelector("[data-delta]")).toBeNull();
});

test("Load older asks for before= the smallest id shown and the page appends under its days without a second header", async () => {
  const first = { rows: ROWS.slice(0, 4), limit: 4, next_before: 228 };
  const older = { rows: ROWS.slice(4), limit: 4, next_before: null };
  const { fetchImpl, requests } = pagedFetch({ "": first, "228": older });
  render(<Shipped hosts={[HOST]} now={now} deps={{ fetch: fetchImpl }} tz="UTC" limit={4} />);
  await act(settle);
  expect(requests).toEqual([`${BASE}/shipped?limit=4`]);
  expect(dayHeaders()).toEqual([
    { label: "Today · Sat Sep 5", count: "3 merges", rows: [236, 235, 234] },
    { label: "Yesterday · Fri Sep 4", count: "1 merge", rows: [228] },
  ]);
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("4 merges · last 2 days · median 1.5 rounds (of 4 loaded)");

  fireEvent.click(screen.getByRole("button", { name: "Load older" }));
  await act(settle);
  expect(requests[1]).toBe(`${BASE}/shipped?limit=4&before=228`);
  expect(dayHeaders()).toEqual([
    { label: "Today · Sat Sep 5", count: "3 merges", rows: [236, 235, 234] },
    { label: "Yesterday · Fri Sep 4", count: "2 merges", rows: [228, 227] },
    { label: "Thu Sep 3", count: "2 merges", rows: [224, 223] },
  ]);
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("7 merges · last 3 days · median 2 rounds");
  expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
});

test("a poll after the ledger is exhausted keeps Load older gone and the subtitle whole", async () => {
  const first = { rows: ROWS.slice(0, 4), limit: 4, next_before: 228 };
  const older = { rows: ROWS.slice(4), limit: 4, next_before: null };
  const { fetchImpl, requests } = pagedFetch({ "": first, "228": older });
  const view = render(<Shipped hosts={[HOST]} now={now} deps={{ fetch: fetchImpl }} tz="UTC" limit={4} />);
  await act(settle);
  fireEvent.click(screen.getByRole("button", { name: "Load older" }));
  await act(settle);
  expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();

  view.rerender(<Shipped hosts={[HOST]} now={now} polls={1} deps={{ fetch: fetchImpl }} tz="UTC" limit={4} />);
  await act(settle);
  expect(requests).toEqual([
    `${BASE}/shipped?limit=4`,
    `${BASE}/shipped?limit=4&before=228`,
    `${BASE}/shipped?limit=4`,
  ]);
  expect(dayHeaders().map((group) => group.rows)).toEqual([[236, 235, 234], [228, 227], [224, 223]]);
  expect(document.querySelector("[data-subtitle]")!.textContent).toBe("7 merges · last 3 days · median 2 rounds");
  expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
});

test("an empty page reads Nothing merged yet with no Load older", async () => {
  const { fetchImpl } = pagedFetch({ "": { rows: [], limit: 50, next_before: null } });
  render(<Shipped hosts={[HOST]} now={now} deps={{ fetch: fetchImpl }} tz="UTC" />);
  await act(settle);
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Shipped");
  expect(screen.getByText("Nothing merged yet")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  expect(document.querySelector("[data-subtitle]")).toBeNull();
});
