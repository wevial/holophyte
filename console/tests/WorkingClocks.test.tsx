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
  expect(screen.getByText("wall")).toBeTruthy();
  expect(screen.queryByText("wall 30m 0s")).toBeNull();
  expect(screen.getByText("30m / 10m")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
  // An older daemon's actual_min was wall time, so it cannot imply measured work.
  view.rerender(<ShippedTable rows={[{ ...row, actual_min: 30, working_ms: undefined }]} now={1_800_000} />);
  expect(screen.getByText("wall")).toBeTruthy();
  expect(screen.queryByText("wall 30m 0s")).toBeNull();
  expect(screen.getByText("30m / 10m")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
});

test("working and wall clocks: Board budget agrees with the run row", () => {
  const card = { key: "1", ticket: "KO-458", title: "clocks", project: "factory", status: "in_flight", run, strikesMax: 2 } as BoardCard;
  render(<TicketCard card={card} />);
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("20");
  expect(document.body.textContent).toContain("working 2m 0s / 10m · wall 20m 0s");
});


test("a live run card carries the pending pause note", () => {
  render(<RunRow {...props} run={{ ...run, stop_requested: "reboot writer" }} />);
  expect(screen.getByText("Pause requested: reboot writer")).toBeTruthy();
});

const agentRun: Run = { ...run, time_box_ms: 1_200_000, working_ms: 1_500_000, agent_ms: 720_000, verify_ms: 780_000, verify_started_ms: null };

test("agent clock: the box figures read agent time, not verify, when the daemon serves it", () => {
  const view = render(<RunRow {...props} run={agentRun} />);
  expect(screen.getByText("agent 12m 0s / 20m")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("60");
  // A verify span is open: the agent clock stands still between polls.
  view.rerender(<RunRow {...props} run={{ ...agentRun, verify_started_ms: 2_000 }} sinceMs={60_000} />);
  expect(screen.getByText("agent 12m 0s / 20m")).toBeTruthy();
  view.rerender(<RunRow {...props} run={agentRun} sinceMs={60_000} />);
  expect(screen.getByText("agent 13m 0s / 20m")).toBeTruthy();
  view.unmount();
  const card = { key: "1", ticket: "KO-458", title: "clocks", project: "factory", status: "in_flight", run: agentRun, strikesMax: 2 } as BoardCard;
  render(<TicketCard card={card} />);
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("60");
  expect(document.body.textContent).toContain("agent 12m 0s / 20m · wall 20m 0s");
});

test("agent clock: Shipped sets agent time against the estimate", () => {
  const row: ShippedRow = {
    id: 1, ticket: "KO-458", title: "clocks", rounds: 1, findings: 0,
    started_ms: 0, ended_ms: 1_800_000, actual_min: 25, working_ms: 1_500_000,
    agent_ms: 720_000, verify_ms: 780_000,
    wall_min: 30, estimate_min: 20, merge_sha: null, commit_url: null,
    host: "writer", project: "factory",
  };
  render(<ShippedTable rows={[row]} now={1_800_000} />);
  expect(screen.getByText("agent")).toBeTruthy();
  expect(screen.getByText("12m / 20m")).toBeTruthy();
  expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("60");
});
