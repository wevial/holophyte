import { afterEach, expect, mock, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Btn } from "../../src/next/components/Btn";
import { Section } from "../../src/next/components/Section";
import { StageChip } from "../../src/next/components/StageChip";
import { StateDot } from "../../src/next/components/StateDot";
import { STATES, type State } from "../../src/next/state";

afterEach(cleanup);

const sheet = new DOMParser().parseFromString(
  await Bun.file(new URL("../../design/Comp-Cells.dc.html", import.meta.url)).text(),
  "text/html",
);
const themeCss = await Bun.file(new URL("../../src/next/theme.css", import.meta.url)).text();
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

test("a Section's label folds its children away and back, reporting each fold", () => {
  const onToggle = mock((_open: boolean) => {});
  render(
    <Section label="Needs you" count={5} summary="2 questions, 3 failed" onToggle={onToggle}>
      <p>HOLO-9 asks a question</p>
    </Section>,
  );
  const label = screen.getByRole("button", { name: "Needs you" });
  expect(screen.getByText("5")).toBeTruthy();
  expect(screen.getByText("2 questions, 3 failed")).toBeTruthy();
  fireEvent.click(label);
  expect(label.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByText("HOLO-9 asks a question")).toBeNull();
  expect(onToggle).toHaveBeenLastCalledWith(false);
  expect(document.getElementById(label.getAttribute("aria-controls") ?? "")?.hidden).toBe(true);
  fireEvent.click(label);
  expect(label.getAttribute("aria-expanded")).toBe("true");
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
