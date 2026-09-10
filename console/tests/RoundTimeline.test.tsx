import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { RoundTimeline } from "../src/components/RoundTimeline";
import type { Segment } from "../src/lib/timeline";

const MINUTE = 60_000;
const T = 1_756_900_000_000;

/** A 40 px implement segment on a 1000 px bar (4% of a 30 m box is 1m 12s),
 *  then a review that has run 20 m and is still going. */
const NARROW_FIRST: Segment[] = [
  { kind: "implement", label: "implement", from: T, to: T + 1.2 * MINUTE, running: false, width: 0.04 },
  { kind: "review", label: "review 1", from: T + 1.2 * MINUTE, to: T + 21.2 * MINUTE, running: true, width: 20 / 30 },
];

/** The bar's container in the fixture: 1000 px, so a 4 % segment is 40 px. */
const BAR_PX = 1000;

/** happy-dom lays nothing out, so resolve the component's `calc(A% - Bpx)`
 *  width against the fixture's container width the way the browser would. */
const resolvePx = (calc: string, containerPx: number): number => {
  const match = /^calc\(([\d.]+)% - ([\d.]+)px\)$/.exec(calc);
  if (!match) throw new Error(`not a share width: ${calc}`);
  return (Number(match[1]) / 100) * containerPx - Number(match[2]);
};

const renderInBar = () =>
  render(
    <div data-fixture-bar style={{ width: `${BAR_PX}px` }}>
      <RoundTimeline segments={NARROW_FIRST} />
    </div>,
  );

afterEach(cleanup);

test("the labels are a legend beneath the bar: one entry per phase in order, no widths, a dot in the phase colour", () => {
  renderInBar();
  const labels = document.querySelector("[data-segment-labels]") as HTMLElement;
  expect(labels.className).toContain("flex-wrap");
  const cells = Array.from(labels.querySelectorAll("[data-segment-label]")) as HTMLElement[];
  expect(cells.map((cell) => cell.getAttribute("data-segment-label"))).toEqual(["implement", "review"]);
  expect(cells[0]!.textContent).toBe("implement1m 12s");
  for (const cell of cells) {
    expect(cell.style.width).toBe("");
    expect(cell.style.minWidth).toBe("");
  }
  expect(cells[0]!.querySelector("span")!.className).toContain("bg-accent");
  expect(cells[1]!.querySelector("span")!.className).toContain("bg-review");
  const bar = screen.getByRole("list", { name: "Round timeline" });
  expect((bar.querySelector("li") as HTMLElement).getAttribute("title")).toBe("implement · 1m 12s");
});

test("a phase shorter than a second is drawn in the bar but left out of the legend", () => {
  render(
    <RoundTimeline
      segments={[
        { kind: "implement", label: "implement", from: 0, to: 400, width: 0.001, running: false },
        { kind: "implement", label: "implement", from: 400, to: 60_400, width: 0.5, running: false },
      ]}
    />,
  );
  const bar = screen.getByRole("list", { name: "Round timeline" });
  expect(bar.querySelectorAll("li[data-segment='implement']").length).toBe(2);
  const cells = Array.from(document.querySelectorAll("[data-segment-label]"));
  expect(cells.length).toBe(1);
  expect(cells[0]!.textContent).toBe("implement1m 00s");
});

test("the bar keeps its proportional widths and its remainder; the labels are not inside it", () => {
  renderInBar();
  const bar = screen.getByRole("list", { name: "Round timeline" });
  const items = Array.from(bar.querySelectorAll("li")) as HTMLElement[];
  expect(items.map((item) => item.getAttribute("data-segment"))).toEqual(["implement", "review", "remaining"]);
  expect(Math.round(resolvePx(items[0]!.style.width, BAR_PX))).toBe(40);
  expect(Math.round(resolvePx(items[1]!.style.width, BAR_PX))).toBe(Math.round((20 / 30) * BAR_PX - 4));
  expect(items[1]!.getAttribute("data-running")).toBe("true");
  expect(bar.textContent).toBe("");
});
