import { expect, test } from "bun:test";
import { boxPercent, groupByProject, phaseLabel } from "../src/lib/runs";
import type { Status } from "../src/lib/types";
import { fixture } from "./harness";

const PR_URL = "https://github.com/example/repo/pull/453";

test("only merge_gate with a PR URL is monitoring PR", () => {
  expect(phaseLabel("merge_gate", PR_URL)).toBe("monitoring PR");
  for (const url of [null, undefined, ""]) expect(phaseLabel("merge_gate", url)).toBe("verifying");
  for (const url of [PR_URL, null, undefined, ""]) {
    expect(phaseLabel("verifying", url)).toBe("verifying");
    expect(phaseLabel("working", url)).toBe("implementing");
    expect(phaseLabel("reviewing", url)).toBe("reviewing");
  }
});

const working = await fixture<Status>("working.json");

test("the bar's share of the box is its percent and never wider than 100", () => {
  const box = 1_800_000;
  const at = (share: number) => boxPercent(box * share, box);
  expect(at(0.5)).toBe(50);
  expect(at(1.2)).toBe(100);
});

test("phases fold into the three working words; anything else keeps its name", () => {
  expect(phaseLabel("working")).toBe("implementing");
  expect(phaseLabel("addressing")).toBe("implementing");
  expect(phaseLabel("verifying")).toBe("verifying");
  expect(phaseLabel("merge_gate")).toBe("verifying");
  expect(phaseLabel("reviewing")).toBe("reviewing");
  expect(phaseLabel("closing_out")).toBe("closing_out");
});

test("groupByProject keys one block per project path and daemon, and names it by its last segment", () => {
  const other: Status = { ...working, project: "/srv/dev/other", runs: [{ ...working.runs[0]!, id: 7 }] };
  const writer = "http://writer:7710";
  const second = "http://second:7710";
  const groups = groupByProject([
    { base: writer, status: working },
    { base: writer, status: other },
    { base: writer, status: working },
    { base: second, status: working },
  ]);
  expect(groups.map((group) => [group.path, group.name, group.base, group.runs.map((run) => run.id)])).toEqual([
    ["/srv/dev/writer", "writer", writer, [91, 91]],
    ["/srv/dev/other", "other", writer, [7]],
    ["/srv/dev/writer", "writer", second, [91]],
  ]);
});
