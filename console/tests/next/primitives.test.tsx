import { afterEach, expect, mock, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { Btn } from "../../src/next/components/Btn";
import { Section } from "../../src/next/components/Section";
import { StageChip } from "../../src/next/components/StageChip";
import { StateDot } from "../../src/next/components/StateDot";

afterEach(cleanup);

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
