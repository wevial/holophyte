import { afterEach, expect, mock, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ConfirmDialog } from "../../src/app/components/ConfirmDialog";
import { Btn } from "../../src/app/components/Btn";
import { BarChart, Change, LineChart } from "../../src/app/components/FlowCharts";
import { Section } from "../../src/app/components/Section";
import { StageChip } from "../../src/app/components/StageChip";
import { StateDot } from "../../src/app/components/StateDot";
import { flowWeekly, weeklyTips } from "../../src/app/flow";
import { STATES, type State } from "../../src/app/state";

afterEach(cleanup);

const sheet = new DOMParser().parseFromString(
  await Bun.file(new URL("../../design/Comp-Cells.dc.html", import.meta.url)).text(),
  "text/html",
);
const themeCss = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
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

test("busy and disabled buttons swallow clicks, the busy one says so, and an idle one fires once", () => {
  const busy = mock(() => {});
  const disabled = mock(() => {});
  const idle = mock(() => {});
  render(
    <>
      <Btn variant="primary" busy busyLabel="Merging…" onClick={busy}>Merge</Btn>
      <Btn disabled onClick={disabled}>Requeue</Btn>
      <Btn onClick={idle}>Resume</Btn>
    </>,
  );
  const merging = screen.getByRole("button", { name: "Merging…" });
  fireEvent.click(merging);
  fireEvent.click(screen.getByRole("button", { name: "Requeue" }));
  fireEvent.click(screen.getByRole("button", { name: "Resume" }));
  expect(merging.getAttribute("aria-busy")).toBe("true");
  expect(busy).not.toHaveBeenCalled();
  expect(disabled).not.toHaveBeenCalled();
  expect(idle).toHaveBeenCalledTimes(1);
});

test("a Section's label folds its children away and back, turning its chevron and reporting each fold", () => {
  const onToggle = mock((_open: boolean) => {});
  render(
    <Section label="Needs you" count={5} summary="2 questions, 3 failed" onToggle={onToggle}>
      <p>HOLO-9 asks a question</p>
    </Section>,
  );
  const label = screen.getByRole("button", { name: "Needs you" });
  const turned = () => label.querySelector("svg[data-chevron]")!.getAttribute("class")!.split(/\s+/).includes("rotate-90");
  expect(screen.getByText("5")).toBeTruthy();
  expect(turned()).toBe(true);
  expect(screen.getByText("2 questions, 3 failed")).toBeTruthy();
  fireEvent.click(label);
  expect(label.getAttribute("aria-expanded")).toBe("false");
  expect(turned()).toBe(false);
  expect(screen.queryByText("HOLO-9 asks a question")).toBeNull();
  expect(onToggle).toHaveBeenLastCalledWith(false);
  expect(document.getElementById(label.getAttribute("aria-controls") ?? "")?.hidden).toBe(true);
  fireEvent.click(label);
  expect(label.getAttribute("aria-expanded")).toBe("true");
  expect(turned()).toBe(true);
  expect(screen.getByText("HOLO-9 asks a question")).toBeTruthy();
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

test("disabled and busy buttons keep their own variant's edge and label, and a disabled one is at half opacity", () => {
  const style = document.createElement("style");
  style.textContent = themeCss.replace(/@import[^;]*;/, "");
  document.head.appendChild(style);
  try {
    const token = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    for (const [variant, edge] of [["primary", "--act"], ["quiet", "--ink-3"], ["danger", "--bad"]] as const) {
      const colour = token(edge);
      render(
        <>
          <Btn variant={variant} disabled>Off</Btn>
          <Btn variant={variant} busy>On</Btn>
        </>,
      );
      const disabled = getComputedStyle(screen.getByRole("button", { name: "Off" }));
      const busy = getComputedStyle(screen.getByRole("button", { name: "On" }));
      expect(disabled.boxShadow, variant).toBe(`inset 0 0 0 1px ${colour}`);
      expect(disabled.opacity, variant).toBe("0.5");
      // happy-dom resolves only the first var() in a multi-layer value.
      const dashes = [...busy.backgroundImage.matchAll(/linear-gradient\(\w+, (\S+) 50%/g)].map((m) => m[1]);
      expect(dashes, variant).toEqual([colour, "var(--btn-edge)", "var(--btn-edge)", "var(--btn-edge)"]);
      expect(busy.color, variant).toBe(colour);
      cleanup();
    }
    render(<Btn variant="danger-fill" disabled>Abort run</Btn>);
    expect(getComputedStyle(screen.getByRole("button", { name: "Abort run" })).color).toBe(token("--bad-ink"));
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
