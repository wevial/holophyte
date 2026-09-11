import { expect, test } from "bun:test";
import { findingParts, findingPath, severityOf } from "../src/lib/findings";
import type { Finding } from "../src/lib/types";

const finding = (over: Partial<Finding>): Finding => ({
  path: "holophyte/loop.py",
  severity: "p2",
  message: "",
  ...over,
});

test("severityOf reads the reviewer's p0/p1/p2/nit as must/must/should/nit", () => {
  for (const [severity, pill] of [
    ["p0", "must"],
    ["p1", "must"],
    ["P1", "must"],
    ["p2", "should"],
    ["nit", "nit"],
    ["anything-else", "nit"],
  ] as const) {
    expect(severityOf(finding({ severity }))).toBe(pill);
  }
});

test("findingPath strips either container mount and leaves a repository path alone", () => {
  expect(findingPath(finding({ path: "/workspace/holophyte/loop.py" }))).toBe("holophyte/loop.py");
  expect(findingPath(finding({ path: "/home/reviewer/candidate/holophyte/loop.py" }))).toBe("holophyte/loop.py");
  expect(findingPath(finding({ path: "holophyte/loop.py" }))).toBe("holophyte/loop.py");
});

test("a [P1] bullet splits into the bold lead, a dropped location link and the remainder", () => {
  const parts = findingParts(
    "- [P1] **Conflicted merge fails the run instead of returning to the implementer** — " +
      "[holophyte/loop.py](/home/reviewer/candidate/holophyte/loop.py:345) returns before " +
      "`release()` runs on the conflict path",
  );
  expect(parts.title).toBe("Conflicted merge fails the run instead of returning to the implementer");
  expect(parts.body).toBe("returns before `release()` runs on the conflict path");
  expect(parts.criterion).toBeNull();
});

test("a plain sentence has no title and its message is the body", () => {
  const parts = findingParts("The merge commit message does not name the conflicted ref");
  expect(parts.title).toBeNull();
  expect(parts.body).toBe("The merge commit message does not name the conflicted ref");
  expect(parts.criterion).toBeNull();
});

test("a CRITERION line titles Criterion n · status with the reason as body and the criterion folded", () => {
  const parts = findingParts(
    "CRITERION 2: not met — no test exercises the conflict path\n" +
      "Given a merge-gate conflict, when the gate runs, then the run goes back " +
      "to the implementer (tests/test_gates.py witnesses it)",
  );
  expect(parts.title).toBe("Criterion 2 · not met");
  expect(parts.body).toBe("no test exercises the conflict path");
  expect(parts.criterion).toBe(
    "Given a merge-gate conflict, when the gate runs, then the run goes back " +
      "to the implementer (tests/test_gates.py witnesses it)",
  );
});
