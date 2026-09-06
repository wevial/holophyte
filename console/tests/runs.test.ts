import { expect, test } from "bun:test";
import { boxPercent, boxTone, groupByProject, phaseLabel, strikeTone } from "../src/lib/runs";
import type { Status } from "../src/lib/types";
import { fixture } from "./harness";

const working = await fixture<Status>("working.json");

test("the bar is teal under 70 % of the box, amber from 70 %, red at 100 %, and never wider than 100", () => {
  const box = 1_800_000;
  const at = (share: number) => boxPercent(box * share, box);
  expect(boxTone(at(0.5))).toBe("teal");
  expect(boxTone(at(0.7))).toBe("amber");
  expect(boxTone(at(0.99))).toBe("amber");
  expect(boxTone(at(1.2))).toBe("red");
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

test("strikes are amber until the run stands on its last one, red from there, nothing with none", () => {
  expect(strikeTone(0, 3)).toBeNull();
  expect(strikeTone(1, 3)).toBe("amber");
  expect(strikeTone(2, 3)).toBe("red");
  expect(strikeTone(3, 3)).toBe("red");
  expect(strikeTone(1, 4)).toBe("amber");
});

test("groupByProject keys one block per project path and daemon, and names it by its last segment", () => {
  const other: Status = { ...working, target: "/srv/dev/other", runs: [{ ...working.runs[0]!, id: 7 }] };
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
