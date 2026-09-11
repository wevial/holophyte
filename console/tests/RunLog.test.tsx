import { afterEach, expect, test } from "bun:test";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { RunLog } from "../src/components/RunLog";
import { formatClock } from "../src/lib/format";
import type { RunEvent } from "../src/lib/types";

const T = 1_756_900_000_000;
const MINUTE = 60_000;
const NOW = T + 20 * MINUTE;

/** Six narrative events, the last a heartbeat 4 s before `NOW`. */
const SIX: RunEvent[] = [
  { at: T, kind: "claimed", summary: "claimed KO-232" },
  { at: T + 1 * MINUTE, kind: "phase", summary: "implementing" },
  { at: T + 8 * MINUTE, kind: "review", summary: "review round 1 opened" },
  { at: T + 12 * MINUTE, kind: "review", summary: "round 1: changes requested" },
  { at: T + 16 * MINUTE, kind: "review", summary: "review round 2 opened" },
  { at: NOW - 4_000, kind: "heartbeat", summary: "heartbeat" },
];

afterEach(cleanup);

const rows = () => Array.from(document.querySelectorAll("[data-log-row]")) as HTMLElement[];

test("six events read 6 events · last: heartbeat 4s ago with the first→last range, folded until the header opens them", () => {
  render(<RunLog events={SIX} now={NOW} />);
  expect(document.querySelector("[data-log-summary]")!.textContent).toBe("6 events · last: heartbeat 4s ago");
  expect(document.querySelector("[data-log-range]")!.textContent).toBe(`${formatClock(T)} → ${formatClock(NOW - 4_000)}`);
  const header = screen.getByRole("button");
  const chevron = document.querySelector("[data-chevron]")!;
  expect(header.getAttribute("aria-expanded")).toBe("false");
  expect(chevron.getAttribute("data-chevron")).toBe("closed");
  expect(chevron.textContent).toBe("▸");
  expect(document.querySelector("[data-log-rows]")).toBeNull();
  fireEvent.click(header);
  expect(rows().length).toBe(6);
  expect(rows().map((row) => row.children[1]!.textContent)).toEqual(SIX.map((event) => event.summary));
  expect(rows()[0]!.children[0]!.textContent).toBe(formatClock(T));
  expect(header.getAttribute("aria-expanded")).toBe("true");
  expect(chevron.getAttribute("data-chevron")).toBe("open");
  expect(chevron.textContent).toBe("▾");
  fireEvent.click(header);
  expect(rows().length).toBe(0);
  expect(document.querySelector("[data-log-rows]")).toBeNull();
  expect(document.querySelector("[data-log-summary]")!.textContent).toBe("6 events · last: heartbeat 4s ago");
});

test("rows are newest last whatever order the wire sent", () => {
  render(<RunLog events={[SIX[5]!, SIX[0]!, SIX[2]!]} now={NOW} />);
  fireEvent.click(screen.getByRole("button"));
  expect(rows().map((row) => row.children[1]!.textContent)).toEqual(["claimed KO-232", "review round 1 opened", "heartbeat"]);
});

test("no events reads No events yet with no rows and no range", () => {
  render(<RunLog events={[]} now={NOW} />);
  expect(document.querySelector("[data-log-summary]")!.textContent).toBe("No events yet");
  expect(document.querySelector("[data-log-range]")!.textContent).toBe("");
  expect(document.querySelector("[data-log-rows]")).toBeNull();
});
