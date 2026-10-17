import { expect, test } from "bun:test";
import type { RunCriterion } from "../src/lib/contract.gen";
import { criteriaRows } from "../src/lib/criteria";
import { summaryOf } from "../src/lib/ticket";

const rows = [{ id: "C1", text: "first" }, { id: "C2", text: "second" }, { id: "C4", text: "fourth" }];

test("criteriaRows joins verdicts by id across retired criteria and preserves unjudged standings", () => {
  const judged: RunCriterion[] = [
    { id: "C4", n: 4, text: "fourth", status: "not_met", note: "Missing coverage", run: 12, round: 1 },
    { id: "C1", n: 1, text: "first", status: "met", note: "Verified", run: 12, round: 1 },
  ];
  for (const entries of [judged, [...judged].reverse()]) {
    expect(criteriaRows(rows, entries, true)).toEqual([
      { id: "C1", text: "first", standing: "met" },
      { id: "C2", text: "second", standing: "open" },
      { id: "C4", text: "fourth", standing: "unmet" },
    ]);
  }
  expect(criteriaRows(rows, [], false).map((row) => row.standing)).toEqual(["todo", "todo", "todo"]);
  expect(criteriaRows(rows, [{ id: "C2", status: "unwitnessed" }], true)[1]?.standing).toBe("open");
});

test("summaryOf answers only the Summary section's text, and empty for a body without one", () => {
  const body = [
    "# Cache the board read",
    "",
    "## Summary",
    "",
    "The board query reads every ticket on each poll.",
    "Cache it for one poll interval.",
    "",
    "## What / Why / How",
    "",
    "**What:** a cache.",
  ].join("\n");
  expect(summaryOf(body)).toBe("The board query reads every ticket on each poll.\nCache it for one poll interval.");
  expect(summaryOf("# A title\n\n## What / Why / How\n\n**What:** a cache.")).toBe("");
});
