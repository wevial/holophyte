import { expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { createElement } from "react";
import { StateDot } from "../../src/next/components/StateDot";
import { stateOf } from "../../src/next/state";

const STALE_MS = 60_000;

test("attention items read their park, the stuck and supervisor kinds need you, and an unknown kind needs you", () => {
  const pr = (park_kind: string) => ({ kind: "pr_open", level: "attention", park_kind });
  expect([
    stateOf({ attention: pr("ci") }),
    stateOf({ attention: pr("pull_request"), readiness: { ready: true } }),
    stateOf({ attention: pr("pull_request") }),
    stateOf({ attention: { kind: "stuck_wait" } }),
    stateOf({ attention: { kind: "supervisor" } }),
    stateOf({ attention: { kind: "unreachable" } }),
    stateOf({ attention: { kind: "later_kind" } }),
  ]).toEqual(["on_ci", "ready_to_merge", "parked", "needs_you", "needs_you", "stale", "needs_you"]);
});

test("a live run is stale only past the heartbeat threshold, an ended run reads its outcome, and board states map", () => {
  const live = (heartbeat_age_ms: number) => ({ run: { phase: "reviewing", heartbeat_age_ms }, staleMs: STALE_MS });
  const ended = (phase: string, outcome: string) => ({ run: { phase, outcome }, staleMs: STALE_MS });
  expect([
    stateOf(live(STALE_MS - 1)),
    stateOf(live(STALE_MS + 1)),
    stateOf(ended("done", "merged")),
    stateOf(ended("failed", "failed")),
    stateOf(ended("paused", "paused")),
  ]).toEqual(["running", "stale", "merged", "failed", "paused"]);
  expect((["backlog", "blocked_on_deps", "ready", "in_flight"] as const).map((board) => stateOf({ board })))
    .toEqual(["backlog", "todo", "queued", "running"]);
});

test("an ended run abandoned reads abandoned, and its dot names it in the ink-3 tone", () => {
  const state = stateOf({ run: { phase: "done", outcome: "abandoned" }, staleMs: STALE_MS });
  expect(state).toBe("abandoned");
  render(createElement(StateDot, { state }));
  const dot = screen.getByRole("img", { name: "abandoned" });
  expect(dot.className).toContain("text-ink-3");
  expect(dot.textContent).toBe("abandoned");
  cleanup();
});
