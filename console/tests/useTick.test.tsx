import { afterEach, expect, setSystemTime, test } from "bun:test";
import { act, cleanup, render } from "@testing-library/react";
import { useTick } from "../src/hooks/useTick";
import { captureIntervals } from "./harness";

afterEach(cleanup);

function Probe({ onRender, ms = 1000 }: { onRender: (now: number) => void; ms?: number }) {
  onRender(useTick(ms));
  return null;
}

test("the caller re-renders on each interval, reading the local clock as it goes", () => {
  const timers = captureIntervals();
  setSystemTime(1_756_900_000_000);
  try {
    const renders: number[] = [];
    render(<Probe onRender={(now) => renders.push(now)} />);
    expect(renders).toEqual([1_756_900_000_000]);
    expect(timers.pending.size).toBe(1);
    setSystemTime(1_756_900_001_000);
    act(() => timers.fire());
    setSystemTime(1_756_900_002_000);
    act(() => timers.fire());
    expect(renders).toEqual([1_756_900_000_000, 1_756_900_001_000, 1_756_900_002_000]);
  } finally {
    setSystemTime();
    timers.restore();
  }
});

test("unmount clears the interval, so nothing renders after", () => {
  const timers = captureIntervals();
  try {
    const renders: number[] = [];
    const view = render(<Probe onRender={() => renders.push(1)} />);
    act(() => timers.fire());
    expect(renders.length).toBe(2);
    const orphaned = [...timers.pending.values()];
    view.unmount();
    expect(timers.pending.size).toBe(0);
    expect(timers.cleared.length).toBe(1);
    act(() => orphaned.forEach((fn) => fn()));
    expect(renders.length).toBe(2);
  } finally {
    timers.restore();
  }
});
