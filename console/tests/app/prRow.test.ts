import { expect, test } from "bun:test";
import type { AttentionItem } from "../../src/lib/types";
import { prWait } from "../../src/app/prRow";

const MINUTE = 60_000;
const parkOn = (parkKind: string, pr: object): AttentionItem => ({
  kind: "pr_open", level: "attention", run: 12, ticket: "DEMO-12", park_kind: parkKind,
  pr_url: "https://github.com/example/demo/pull/45", pr: { state: "ready", ...pr },
});

test("a draft parked on CI reads On CI with what CI is doing, not Draft", () => {
  const wait = prWait("on_ci", parkOn("ci", { state: "draft", checks: "pending" }), 20 * MINUTE);
  expect([wait.status, wait.line]).toEqual(["On CI", "checks running · 20m"]);
});

test("a draft held for review reads Draft over any not-ready reason its readiness gives", () => {
  const wait = prWait("parked", parkOn("pull_request", { state: "draft", checks: "pending" }), 20 * MINUTE, "checks_pending");
  expect([wait.status, wait.line]).toEqual(["Draft · 20m", ""]);
});

test("an On CI line drops what it cannot name: failing checks leave the age alone, and no age leaves the CI words alone", () => {
  const failing = prWait("on_ci", parkOn("ci", { checks: "failure" }), 20 * MINUTE);
  expect([failing.status, failing.line]).toEqual(["On CI", "20m"]);
  const unaged = prWait("on_ci", parkOn("ci", { checks: "success" }), null);
  expect(unaged.line).toBe("checks green, quiet period");
});
