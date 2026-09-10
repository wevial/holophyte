import { afterEach, expect, test } from "bun:test";
import { cleanup, render } from "@testing-library/react";
import { ShippedTable } from "../src/components/ShippedTable";
import { tagRows } from "../src/lib/shipped";
import type { ShippedBody, ShippedRow } from "../src/lib/types";
import { fixture } from "./harness";

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
