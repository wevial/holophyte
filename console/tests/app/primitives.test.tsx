import { afterEach, expect, mock, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ConfirmDialog } from "../../src/app/components/ConfirmDialog";
import { Btn } from "../../src/app/components/Btn";
import { BarChart, Change, LineChart, Sparkline } from "../../src/app/components/FlowCharts";
import { Section } from "../../src/app/components/Section";
import { StageChip } from "../../src/app/components/StageChip";
import { StateDot } from "../../src/app/components/StateDot";
import { RunActivity } from "../../src/app/components/RunActivity";
import { runActivity } from "../../src/app/activity";
import { flowWeekly, weeklyTips } from "../../src/app/flow";
import type { Round, RunDetail } from "../../src/lib/contract.gen";
import { SERVE } from "./needsFleet";
import { STATES, type State } from "../../src/app/state";

afterEach(cleanup);

const sheet = new DOMParser().parseFromString(
  await Bun.file(new URL("../../design/Comp-Cells.dc.html", import.meta.url)).text(),
  "text/html",
);
const themeCss = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
const served = (await Bun.file(new URL("run-detail.json", SERVE)).json()) as RunDetail;
const GEOMETRY = ["d", "cx", "cy", "r", "x", "y", "width", "height", "pathLength", "stroke-dasharray"];

function shapes(svg: Element) {
  return [...svg.children].map((shape) => [
    shape.tagName.toLowerCase(),
    ...GEOMETRY.map((name) => shape.getAttribute(name)),
  ]);
}

test("each StateDot on the State icons sheet's Run states grid draws that cell's glyph", () => {
  const cells = [...sheet.querySelectorAll('section[aria-label="Run states"] > div')];
  expect(cells.length).toBe(12);
  const byWord = new Map(Object.entries(STATES).map(([state, { word }]) => [word, state as State]));
  for (const cell of cells) {
    const word = cell.querySelector("svg + div > div")!.textContent!;
    const state = byWord.get(word);
    expect(state, word).toBeDefined();
    const { container } = render(<StateDot state={state!} />);
    expect(shapes(container.querySelector("svg")!), word).toEqual(shapes(cell.querySelector("svg")!));
    cleanup();
  }
  const glyph = (state: State) => render(<StateDot state={state} />).container.querySelector("svg")!;
  const merged = glyph("merged");
  expect(merged.querySelector("circle")!.getAttribute("fill")).toBe("currentColor");
  const running = glyph("running");
  expect(running.querySelector("circle")!.getAttribute("fill")).toBeNull();
  expect(running.querySelector("path")!.getAttribute("fill")).toBe("currentColor");
});

test("a StateDot with its word hidden keeps the word as its accessible name and hides its icon", () => {
  render(<StateDot state="on_ci" hideWord />);
  const dot = screen.getByRole("img", { name: "on CI" });
  expect(dot.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
  expect(screen.queryByText("on CI")).toBeNull();
});

test("a Section heads its rows with its name and count and no fold, says its empty line at zero, and draws only its heading while uncounted", () => {
  const section = (count?: number) => (
    <Section label="Waiting" count={count} empty="Nothing waits on a pull request." footer={<p>witness line</p>}>
      <ul><li>HOLO-405</li></ul>
    </Section>
  );
  const { rerender } = render(section(2));
  const region = screen.getByRole("region", { name: "Waiting" });
  expect(region.querySelector("header")!.textContent).toBe("Waiting2");
  expect(screen.queryByRole("button")).toBeNull();
  expect(screen.getByText("HOLO-405")).toBeTruthy();
  expect(screen.getByText("witness line")).toBeTruthy();
  rerender(section(0));
  const line = screen.getByText("Nothing waits on a pull request.");
  expect(line.textContent).toBe("Nothing waits on a pull request.");
  expect(line.querySelector("svg")!.getAttribute("aria-hidden")).toBe("true");
  expect(screen.queryByText("HOLO-405")).toBeNull();
  rerender(section(undefined));
  expect(region.textContent).toBe("Waiting");
});

test("a StageChip reads the review round against its cap, and a merge gate with a PR without a round", () => {
  const { container: reviewing } = render(<StageChip run={{ phase: "reviewing", round: 2, max_rounds: 3 }} />);
  const { container: gate } = render(
    <StageChip run={{ phase: "merge_gate", round: 2, max_rounds: 3, pr_url: "https://example.test/pr/7" }} />,
  );
  expect(reviewing.textContent).toBe("reviewing · round 2 of 3");
  expect(gate.textContent).toBe("monitoring PR");
});

test("a StageChip given a label draws its state's icon and its sub after a dot, a running icon by default", () => {
  const { container: merged } = render(<StageChip label="Merged · 5acc138" state="merged" />);
  const { container: review } = render(<StageChip label="Review · round 2 of 3" sub="1 loop back" />);
  expect(merged.textContent).toBe("Merged · 5acc138");
  expect(merged.querySelector("[data-state]")!.getAttribute("data-state")).toBe("merged");
  expect(review.textContent).toBe("Review · round 2 of 3 · 1 loop back");
  expect(review.querySelector("[data-state]")!.getAttribute("data-state")).toBe("running");
});

test("each variant's disabled button is at half opacity and its busy one says its busy label, both swallowing clicks, while an idle one fires once", () => {
  const style = document.createElement("style");
  style.textContent = themeCss.replace(/@import[^;]*;/, "");
  document.head.appendChild(style);
  try {
    for (const variant of ["primary", "need", "quiet", "danger", "danger-fill"] as const) {
      const disabledClick = mock(() => {});
      const busyClick = mock(() => {});
      render(
        <>
          <Btn variant={variant} disabled onClick={disabledClick}>Requeue</Btn>
          <Btn variant={variant} busy busyLabel="Merging…" onClick={busyClick}>Merge</Btn>
        </>,
      );
      const disabled = screen.getByRole("button", { name: "Requeue" });
      const busy = screen.getByRole("button", { name: "Merging…" });
      fireEvent.click(disabled);
      fireEvent.click(busy);
      expect(getComputedStyle(disabled).opacity, variant).toBe("0.5");
      expect(busy.getAttribute("aria-busy"), variant).toBe("true");
      expect(screen.queryByText("Merge"), variant).toBeNull();
      expect(disabledClick, variant).not.toHaveBeenCalled();
      expect(busyClick, variant).not.toHaveBeenCalled();
      cleanup();
    }
    const idle = mock(() => {});
    render(<Btn onClick={idle}>Resume</Btn>);
    fireEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(idle).toHaveBeenCalledTimes(1);
  } finally {
    style.remove();
  }
});

test("a confirm defaults to Cancel and primary, and supports a focused Keep running cancel with a filled danger action", () => {
  const cancel = mock(() => {});
  const confirm = mock(() => {});
  const props = { label: "Abort · demo", heading: "Abort run 12?", confirm: "Abort run", busyLabel: "Aborting…", busy: false, onCancel: cancel, onConfirm: confirm, children: "The branch is kept." };
  const { rerender } = render(<ConfirmDialog {...props} />);
  expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));
  expect(screen.getByRole("button", { name: "Abort run" }).getAttribute("data-variant")).toBe("primary");
  rerender(<ConfirmDialog {...props} cancelLabel="Keep running" confirmVariant="danger-fill" />);
  const keep = screen.getByRole("button", { name: "Keep running" });
  expect(document.activeElement).toBe(keep);
  expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  const abort = screen.getByRole("button", { name: "Abort run" });
  expect(abort.getAttribute("data-variant")).toBe("danger-fill");
  fireEvent.click(keep);
  expect(cancel).toHaveBeenCalledTimes(1);
  expect(confirm).not.toHaveBeenCalled();
  fireEvent.click(abort);
  expect(confirm).toHaveBeenCalledTimes(1);
});

const WEEK_MS = 7 * 24 * 3600 * 1000;
const SIX_WEEKS = Array.from({ length: 6 }, (_, index) => Date.UTC(2026, 9, 2) + index * WEEK_MS);
const UNTOLD = () => ({ value: "", context: "" });
const NO_WAITS = { pending_checks: 0, checks_failing: 0, thread: 0, rounds_used: 0, behind_main: 0, main_red: 0, approval: 0, other: 0 };

test("the merges chart draws a bar a week, the latest in the accent colour, under a top gridline rounded up to 200", () => {
  const { container } = render(<BarChart title="Merges" note="per week" starts={SIX_WEEKS} values={[57, 123, 173, 133, 20, 43]} format={String} tip={UNTOLD} />);
  const bars = [...container.querySelectorAll("rect")];
  expect(bars.length).toBe(6);
  expect(bars.map((bar) => bar.getAttribute("class"))).toEqual([...Array(5).fill("fill-wait"), "fill-accent"]);
  const heights = bars.map((bar) => Number(bar.getAttribute("height")));
  expect(heights[2]).toBeCloseTo((100 * 173) / 200, 1);
  expect([...container.querySelectorAll("[data-grid]")].map((line) => line.getAttribute("data-grid"))).toEqual(["200", "100", "0"]);
});

test("a waiting line breaks at a week with no merges into two segments and puts no dot on that week", () => {
  const { container } = render(
    <LineChart title="Time spent waiting" note="share of run time" starts={SIX_WEEKS} values={[80, 75, null, 48, 52, 50]} top={100} format={(value) => `${value}%`} tip={UNTOLD} />,
  );
  const lines = [...container.querySelectorAll("path[data-segment]")];
  expect(lines.length).toBe(2);
  expect(lines.every((line) => (line.getAttribute("d")!.match(/M/g) ?? []).length === 1)).toBe(true);
  expect([...container.querySelectorAll("[data-dot]")].map((dot) => dot.getAttribute("data-dot"))).toEqual(["0", "1", "3", "4", "5"]);
  expect(screen.getByRole("figure").getAttribute("aria-label")).toContain("no data");
  expect([...container.querySelectorAll("button[data-point]")].map((point) => point.getAttribute("data-point"))).toEqual(["0", "1", "3", "4", "5"]);
});

test("a sparkline's stroke runs from the left edge to the right edge, and a week standing alone between gaps, or as the only week, still shows a mark", () => {
  const { container: gapped } = render(<Sparkline values={[1, 2, null, 5, null, 3, 4]} />);
  const strokes = [...gapped.querySelectorAll("path")].map((path) => path.getAttribute("d")!);
  expect(strokes).toHaveLength(2);
  expect(strokes[0]!.startsWith("M0 ")).toBe(true);
  expect(strokes[1]!.split(" ").at(-2)).toBe("100");
  expect([...gapped.querySelectorAll("[data-lone]")].map((mark) => (mark as HTMLElement).style.left)).toEqual(["50%"]);
  const { container: single } = render(<Sparkline values={[38]} />);
  expect(single.querySelectorAll("path")).toHaveLength(0);
  expect(single.querySelectorAll("[data-lone]")).toHaveLength(1);
});

test("a weekly change is green when the number moved the good way and the accent colour when not", () => {
  const { container: merged } = render(<dl><Change number="merged" values={[20, 43]} /></dl>);
  const { container: waiting } = render(<dl><Change number="waiting" values={[50, 55]} /></dl>);
  const mergedChange = merged.querySelector("[data-change]")!;
  const waitingChange = waiting.querySelector("[data-change]")!;
  expect(mergedChange.textContent).toBe("+23 vs the week before");
  expect(mergedChange.classList.contains("text-live")).toBe(true);
  expect(waitingChange.textContent).toBe("+5 pts vs the week before");
  expect(waitingChange.classList.contains("text-accent")).toBe(true);
});

test("a week with no merges has no interventions count, so the All projects interventions line breaks there", () => {
  const weeks = [1, 0, 1].map((merged, index) => ({
    start_ms: SIX_WEEKS[index]!,
    merged,
    rounds_median: null,
    interventions: [2, 0, 1][index]!,
    hours: { working: 1, waiting: 1, operator: 0 },
  }));
  const flow = { now: SIX_WEEKS[3]!, weeks, phases: {}, waits: NO_WAITS, reverted: null };
  expect(flowWeekly([flow]).interventions).toEqual([2, null, 1]);
});

test("a per-merge line far below one keeps its gridlines at distinct one-decimal values", () => {
  const { container } = render(
    <LineChart title="Interventions per merge" note="per week" starts={SIX_WEEKS} values={[0.01, 0.02, 0, 0.01, 0, 0.01]} format={(value) => value.toFixed(1)} tip={UNTOLD} />,
  );
  expect([...container.querySelectorAll("[data-grid]")].map((line) => line.getAttribute("data-grid"))).toEqual(["1.0", "0.5", "0.0"]);
});

const WEEKS = [
  { merged: 57, rounds_median: 2, interventions: 6, hours: { working: 30, waiting: 20, operator: 10 } },
  { merged: 0, rounds_median: null, interventions: 3, hours: { working: 4, waiting: 0, operator: 0 } },
  { merged: 173, rounds_median: 3, interventions: 346, hours: { working: 42, waiting: 35, operator: 7 } },
].map((week, index) => ({ start_ms: SIX_WEEKS[index + 2]!, ...week }));
const WEEKLY = flowWeekly([{ now: SIX_WEEKS[5]!, weeks: WEEKS, phases: {}, waits: NO_WAITS, reverted: null }]);

test("focusing a waiting point tooltips its week, share and run-hours, grows its dot and hides the latest label until blur, whatever the mouse does", () => {
  const { container } = render(
    <LineChart title="Time spent waiting" note="share of run time" starts={WEEKLY.starts} values={WEEKLY.waitingShare} top={100} format={String} tip={weeklyTips(WEEKLY, false).waiting} />,
  );
  const point = screen.getByRole("button", { name: "Oct 30 – Nov 5: 50% waiting, 42 of 84 run-hours" });
  expect(container.querySelector("[data-latest]")).not.toBeNull();
  fireEvent.focus(point);
  expect(container.querySelector("[data-tooltip]")!.textContent).toBe("Oct 30 – Nov 550% waiting42 of 84 run-hours");
  expect(container.querySelector('[data-dot="2"]')!.classList.contains("size-3")).toBe(true);
  expect(container.querySelector('[data-dot="0"]')!.classList.contains("size-2")).toBe(true);
  expect(container.querySelector("[data-latest]")).toBeNull();
  fireEvent.mouseEnter(point);
  fireEvent.mouseLeave(point);
  expect(container.querySelector("[data-tooltip]")!.textContent).toBe("Oct 30 – Nov 550% waiting42 of 84 run-hours");
  expect(container.querySelector("[data-latest]")).toBeNull();
  fireEvent.blur(point);
  expect(container.querySelector("[data-tooltip]")).toBeNull();
  expect(container.querySelector("[data-latest]")).not.toBeNull();
});

test("hovering a waiting point draws a full-height guide through that point's x, and none shows while nothing is hovered or focused", () => {
  const { container } = render(
    <LineChart title="Time spent waiting" note="share of run time" starts={SIX_WEEKS} values={[80, 75, 60, 48, 52, 50]} top={100} format={String} tip={UNTOLD} />,
  );
  const third = container.querySelector('button[data-point="2"]')!;
  expect(container.querySelector("[data-guide]")).toBeNull();
  fireEvent.mouseEnter(third);
  const guide = container.querySelector<HTMLElement>("[data-guide]")!;
  expect(parseFloat(guide.style.left)).toBeCloseTo((2.5 / 6) * 100, 6);
  expect(guide.style.left).toBe(container.querySelector<HTMLElement>('[data-dot="2"]')!.style.left);
  expect(guide.classList.contains("inset-y-0")).toBe(true);
  expect(guide.parentElement).toBe(container.querySelector('[data-grid="100"]')!.parentElement);
  fireEvent.mouseLeave(third);
  expect(container.querySelector("[data-guide]")).toBeNull();
  fireEvent.focus(third);
  expect(container.querySelector("[data-guide]")).not.toBeNull();
  fireEvent.blur(third);
  expect(container.querySelector("[data-guide]")).toBeNull();
});

test("a hovered merges bar grows and names its median review rounds for one project and its interventions under All projects", () => {
  const bars = (allProjects: boolean) =>
    render(<BarChart title="Merges" note="per week" starts={WEEKLY.starts} values={WEEKLY.merged} format={String} tip={weeklyTips(WEEKLY, allProjects).merged} />);
  const { container: project } = bars(false);
  const widths = () => [...project.querySelectorAll("rect")].map((bar) => Number(bar.getAttribute("width")));
  const restingWidth = widths()[0]!;
  fireEvent.mouseEnter(screen.getByRole("button", { name: "Oct 16 – 22: 57 merged, median 2 review rounds" }));
  expect(project.querySelector("[data-tooltip]")!.textContent).toBe("Oct 16 – 2257 mergedmedian 2 review rounds");
  expect(widths()[0]).toBeGreaterThan(restingWidth);
  expect(widths()[1]).toBe(restingWidth);
  cleanup();
  const { container: all } = bars(true);
  fireEvent.mouseEnter(screen.getByRole("button", { name: "Oct 23 – 29: 0 merged, 3 interventions" }));
  expect(all.querySelector("[data-tooltip]")!.textContent).toBe("Oct 23 – 290 merged3 interventions");
});

test("a rounds point taken over several hosts names the host medians it is the median of, not their summed merges", () => {
  const host = (merged: number, rounds_median: number) => ({
    now: SIX_WEEKS[1]!,
    weeks: [{ start_ms: SIX_WEEKS[0]!, merged, rounds_median, interventions: 0, hours: { working: 1, waiting: 0, operator: 0 } }],
    phases: {},
    waits: NO_WAITS,
    reverted: null,
  });
  const single = flowWeekly([host(100, 1)]);
  const spread = flowWeekly([host(100, 1), host(1, 9)]);
  expect(weeklyTips(single, false).rounds(single.rounds[0]!, 0).context).toBe("median over 100 merges");
  expect(spread.rounds).toEqual([5]);
  expect(weeklyTips(spread, false).rounds(spread.rounds[0]!, 0).context).toBe("median across 2 hosts' medians");
});

const T = Date.UTC(2026, 9, 22, 14, 0);
const MIN = 60_000;

/** A babysat run whose one covering_review step holds a covering round per verdict, ten minutes apart, each begun by a phase change into reviewing; `begun` reviews have begun in all. */
function coveringRun(phase: string, verdicts: string[], begun = verdicts.length): RunDetail {
  const url = "https://github.com/o/r/pull/2400";
  const startOf = (index: number) => T + (index + 1) * 10 * MIN;
  const covering = (verdict: string, index: number): Round =>
    ({ ...served.rounds[0]!, round: index + 1, verdict, instructions: [], operator_notes: [], criteria: [], findings: [], started_ms: startOf(index), ended_ms: startOf(index) + 5 * MIN }) as Round;
  const began = Array.from({ length: begun }, (_, index) => ({ at: startOf(index) - MIN, kind: "phase_change", summary: `${index === 0 ? "merge_gate" : "reviewing"} -> reviewing: review of the fix at f1da4c08e5f4` }));
  return {
    ...served,
    run: { ...served.run, id: 12, phase, started_ms: T, ended_ms: null, outcome: null, max_rounds: 3 },
    events: [
      { at: T, kind: "pull_request", summary: `resuming run 392's candidate holo/x at f1da4c08e5f4 on ${url} for another babysit pass` },
      { at: T + 5 * MIN, kind: "babysit_step", summary: "covering_review" },
      ...began,
    ],
    rounds: verdicts.map(covering),
    steer_notes: [],
    criteria: [],
    precheck: null,
  };
}

const progressOf = (detail: RunDetail) =>
  render(<RunActivity activity={runActivity(detail, [], T + 60 * MIN)} detail={detail} base="http://daemon" project="holophyte" />).container;

test("each round a retried covering review recorded under one collapsed step is its own target in Progress", () => {
  const progress = progressOf(coveringRun("merge_gate", ["changes_requested", "changes_requested", "approved"]));
  const targets = ["round-1", "round-2", "round-3"].map((id) => progress.querySelector(`#${id}`));
  expect(targets.map((target) => target?.getAttribute("data-group"))).toEqual(["covering_review", "covering_review", "covering_review"]);
  expect(targets.map((target) => target!.querySelector("[data-group-title]")!.textContent)).toEqual([
    "Covering review · round 1 · changes requested",
    "Covering review · round 2 · changes requested",
    "Covering review · round 3 · passed",
  ]);
});

test("the covering round a live run is still reviewing is a target in Progress after the recorded ones", () => {
  const progress = progressOf(coveringRun("reviewing", ["changes_requested", "changes_requested"], 3));
  const covering = [...progress.querySelectorAll('[data-group="covering_review"]')];
  expect(covering.map((group) => [group.id, group.hasAttribute("data-running")])).toEqual([["round-1", false], ["round-2", false], ["round-3", true]]);
});

test("an approved covering round still in reviewing while its pull request text refreshes has no next round in Progress", () => {
  const progress = progressOf(coveringRun("reviewing", ["approved"]));
  expect([...progress.querySelectorAll('[data-group="covering_review"]')].map((group) => group.id)).toEqual(["round-1"]);
});
