import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { RunRow } from "../src/components/RunRow";
import { ShippedTable } from "../src/components/ShippedTable";
import { TicketCard } from "../src/components/TicketCard";
import type { Run, ShippedRow } from "../src/lib/types";
import type { BoardCard } from "../src/lib/board";

afterEach(cleanup);
const run: Run = {
  id: 1, ticket: "KO-458", phase: "working", host: "writer",
  heartbeat_age_ms: 1_000, elapsed_ms: 1_200_000, time_box_ms: 600_000,
  working_ms: 120_000, work_started_ms: 1_000,
};
const props = { thresholds: { heartbeat_stale_ms: 300_000, strikes: 2 }, expanded: false, onToggle: () => {} };

test("working and wall clocks: active interpolation, waiting polls, and old daemon", () => {
  const view = render(<RunRow {...props} run={run} sinceMs={60_000} />);
  expect(screen.getByText("working 3m 0s / 10m")).toBeTruthy();
  expect(screen.getByText("wall 21m 0s")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("30");
  expect(screen.getByText("hb 1m 01s")).toBeTruthy();
  const waiting = { ...run, phase: "merge_gate", pr_url: "https://github.com/o/r/pull/1", work_started_ms: null };
  view.rerender(<RunRow {...props} run={waiting} sinceMs={60_000} />);
  expect(screen.getByText("working 2m 0s / 10m")).toBeTruthy();
  expect(screen.getByText("wall 21m 0s")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("20");
  view.rerender(<RunRow {...props} run={{ ...waiting, elapsed_ms: 1_260_000 }} sinceMs={60_000} />);
  expect(screen.getByText("working 2m 0s / 10m")).toBeTruthy();
  expect(screen.getByText("wall 22m 0s")).toBeTruthy();
  view.rerender(<RunRow {...props} run={{ ...run, working_ms: undefined, work_started_ms: undefined }} />);
  expect(screen.getByText("working n/a / 10m")).toBeTruthy();
  expect(screen.getByText("wall 20m 0s")).toBeTruthy();
  expect(screen.getByRole("progressbar").hasAttribute("aria-valuenow")).toBe(false);
});

test("working and wall clocks: Shipped measured and historical durations", () => {
  const row: ShippedRow = {
    id: 1, ticket: "KO-458", title: "clocks", rounds: 1, findings: 0,
    started_ms: 0, ended_ms: 1_800_000, actual_min: 4, working_ms: 240_000,
    wall_min: 30, estimate_min: 10, merge_sha: null, commit_url: null,
    host: "writer", project: "factory",
  };
  const view = render(<ShippedTable rows={[row]} now={1_800_000} />);
  expect(screen.getByText("4m / 10m")).toBeTruthy();
  expect(screen.getByText("wall 30m 0s")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("40");
  view.rerender(<ShippedTable rows={[{ ...row, actual_min: null, working_ms: null }]} now={1_800_000} />);
  expect(screen.getByText("wall 30m 0s")).toBeTruthy();
  expect(screen.queryByRole("progressbar")).toBeNull();
  // An older daemon's actual_min was wall time, so it cannot imply measured work.
  view.rerender(<ShippedTable rows={[{ ...row, actual_min: 30, working_ms: undefined }]} now={1_800_000} />);
  expect(screen.getByText("wall 30m 0s")).toBeTruthy();
  expect(screen.queryByRole("progressbar")).toBeNull();
});

test("working and wall clocks: Board budget agrees with the run row", () => {
  const card = { key: "1", ticket: "KO-458", title: "clocks", project: "factory", status: "in_flight", run, strikesMax: 2 } as BoardCard;
  render(<TicketCard card={card} />);
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("20");
  expect(document.body.textContent).toContain("working 2m 0s / 10m · wall 20m 0s");
});
