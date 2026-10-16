import { expect, test } from "bun:test";
import { criteriaRows } from "../src/lib/criteria";
import { summaryOf } from "../src/lib/ticket";

const texts = ["first", "second", "third", "fourth"];

test("criteriaRows takes each standing from the entry with its n, and a missing entry is open with a run and todo without", () => {
  const judged = [
    { n: 1, status: "met" as const },
    { n: 2, status: "not_met" as const },
    { n: 3, status: "unwitnessed" as const },
  ];
  expect(criteriaRows(texts, judged, true)).toEqual([
    { n: 1, text: "first", standing: "met" },
    { n: 2, text: "second", standing: "unmet" },
    { n: 3, text: "third", standing: "open" },
    { n: 4, text: "fourth", standing: "open" },
  ]);
  expect(criteriaRows(texts, [], false).map((row) => row.standing)).toEqual(["todo", "todo", "todo", "todo"]);
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
