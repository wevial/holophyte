import { afterEach, expect, test } from "bun:test";
import { cleanup, render, screen } from "@testing-library/react";
import { LABEL_MIN_PX, RoundTimeline, labelFits } from "../src/components/RoundTimeline";
import type { Segment } from "../src/lib/timeline";

const MINUTE = 60_000;
const T = 1_756_900_000_000;

/** A 40 px implement segment on a 1000 px bar (4% of a 30 m box is 1m 12s),
 *  then a review that has run 20 m and is still going. */
const NARROW_FIRST: Segment[] = [
  { kind: "implement", label: "implement", from: T, to: T + 1.2 * MINUTE, running: false, width: 0.04 },
  { kind: "review", label: "review 1", from: T + 1.2 * MINUTE, to: T + 21.2 * MINUTE, running: true, width: 20 / 30 },
];

/** The measured bar width the tests pin through the `barPx` seam. */
const BAR_PX = 1000;

/** happy-dom lays nothing out, so resolve the component's `calc(A% - Bpx)`
 *  width against the fixture's container width the way the browser would. */
const resolvePx = (calc: string, containerPx: number): number => {
  const match = /^calc\(([\d.]+)% - ([\d.]+)px\)$/.exec(calc);
  if (!match) throw new Error(`not a share width: ${calc}`);
  return (Number(match[1]) / 100) * containerPx - Number(match[2]);
};

const renderInBar = (barPx = BAR_PX) => render(<RoundTimeline segments={NARROW_FIRST} barPx={barPx} />);

afterEach(cleanup);

test("each label sits under its segment's left edge: the phase and its duration, no colour dot", () => {
  renderInBar();
  const row = document.querySelector("[data-segment-labels]") as HTMLElement;
  expect(row.className).toContain("relative");
  const cells = Array.from(row.querySelectorAll("[data-segment-label]")) as HTMLElement[];
  // The implement segment is 40 px of the 1000 px bar — under LABEL_MIN_PX —
  // so only the running review is labelled, at its 4 % start share.
  expect(cells.map((cell) => cell.getAttribute("data-segment-label"))).toEqual(["review"]);
  expect(cells[0]!.className).toContain("absolute");
  expect(cells[0]!.style.left).toBe("4%");
  const spans = Array.from(cells[0]!.querySelectorAll("span"));
  expect(spans.map((span) => span.textContent)).toEqual(["review 1", "20m 00s"]);
  const bar = screen.getByRole("list", { name: "Round timeline" });
  expect((bar.querySelector("li") as HTMLElement).getAttribute("title")).toBe("implement · 1m 12s");
});

test("labelFits: a segment needs LABEL_MIN_PX of the measured bar, or over 15 % of it unmeasured", () => {
  expect(labelFits(LABEL_MIN_PX / 600, 600)).toBe(true);
  expect(labelFits(0.05, 600)).toBe(false);
  expect(labelFits(0.05, 200)).toBe(false);
  expect(labelFits(0.15, null)).toBe(false);
  expect(labelFits(0.16, null)).toBe(true);
});

test("before the bar is measured only segments over 15 % of it carry a label", () => {
  render(<RoundTimeline segments={NARROW_FIRST} />);
  const cells = document.querySelectorAll("[data-segment-label]");
  expect(cells.length).toBe(1);
  expect(cells[0]!.getAttribute("data-segment-label")).toBe("review");
});

test("a measured bar too narrow for any segment shows no labels", () => {
  renderInBar(100);
  expect(document.querySelectorAll("[data-segment-label]").length).toBe(0);
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
