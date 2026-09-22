import { expect, test } from "bun:test";
import { boxPercent, boxTone, groupByProject, lastActivity, phaseLabel, strikeTone } from "../src/lib/runs";
import type { Status } from "../src/lib/types";
import { fixture } from "./harness";
import { buildTimeline, type TimelineRun } from "../src/lib/timeline";

const PR_URL = "https://github.com/example/repo/pull/453";

test("lastActivity selects the newest action in any order, excluding bookkeeping", () => {
  const phase = { at: 20, kind: "phase_change", summary: "verifying -> awaiting_merge_approval: ready" };
  const opened = { at: 10, kind: "pull_request", summary: "Opened pull request" };
  const bookkeeping = [
    { at: 30, kind: "pr_seen_commits", summary: "[]" },
    { at: 40, kind: "pr_empty_wakes", summary: "2" },
    { at: 50, kind: "pr_wake_breaker", summary: "paused" },
    { at: 60, kind: "pr_text_sha", summary: "abc123" },
  ];
  const events = [bookkeeping[0]!, phase, bookkeeping[1]!, opened, ...bookkeeping.slice(2)];
  for (const ordered of [events, events.toReversed(), events.toSorted((a, b) => a.at - b.at)]) {
    const before = [...ordered];
    expect(lastActivity(ordered)).toEqual(phase);
    expect(ordered).toEqual(before);
  }
  expect(lastActivity(bookkeeping)).toBeNull();
  expect(lastActivity([])).toBeNull();
  expect(lastActivity([...events, { at: 70, kind: "pr_seen", summary: "PR updated" }])?.kind).toBe("pr_seen");
});

test("only merge_gate with a PR URL is monitoring PR", () => {
  expect(phaseLabel("merge_gate", PR_URL)).toBe("monitoring PR");
  for (const url of [null, undefined, ""]) expect(phaseLabel("merge_gate", url)).toBe("verifying");
  for (const url of [PR_URL, null, undefined, ""]) {
    expect(phaseLabel("verifying", url)).toBe("verifying");
    expect(phaseLabel("working", url)).toBe("implementing");
    expect(phaseLabel("reviewing", url)).toBe("reviewing");
  }
});

test("the timeline preserves pre-PR verification and labels the PR wait", () => {
  const run: TimelineRun = {
    started_ms: 0, time_box_ms: 100, phase: "merge_gate", pr_url: PR_URL, rounds: [],
    events: [
      { at: 0, kind: "phase_change", summary: "verifying -> merge_gate: approved" },
      { at: 20, kind: "pull_request", summary: `pull request open: ${PR_URL}` },
      { at: 30, kind: "phase_change", summary: "merge_gate -> merge_gate: babysitting" },
    ],
  };
  expect(buildTimeline(run, 60).map(({ label, from, to }) => ({ label, from, to }))).toEqual([
    { label: "verifying", from: 0, to: 20 },
    { label: "monitoring PR", from: 20, to: 60 },
  ]);
  expect(buildTimeline({ ...run, events: [] }, 60).at(-1)?.label).toBe("verifying");
  const adopted = { ...run, events: run.events!.map((event) => event.kind === "pull_request"
    ? { ...event, summary: `adopted the branch's open pull request: ${PR_URL}` } : event) };
  expect(buildTimeline(adopted, 60).map(({ reason, ...segment }) => segment))
    .toEqual(buildTimeline(run, 60).map(({ reason, ...segment }) => segment));
  expect(buildTimeline(adopted, 60)[1]!.reason).toBe(`adopted the branch's open pull request: ${PR_URL}`);
  const missingOpen = { ...run, events: [
    run.events![0]!,
    { at: 10, kind: "phase_change", summary: "merge_gate -> reviewing: round 2 review" },
    { at: 30, kind: "phase_change", summary: "reviewing -> merge_gate: approved" },
  ] };
  expect(buildTimeline(missingOpen, 60).map((segment) => segment.label)).toEqual([
    "verifying", "review", "monitoring PR",
  ]);
});

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
