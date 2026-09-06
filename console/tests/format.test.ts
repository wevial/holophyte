import { describe, expect, test } from "bun:test";
import { formatAge, formatDuration, formatSpan } from "../src/lib/format";

describe("formatDuration", () => {
  test("renders the ticket's reference durations", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(754000)).toBe("12m 34s");
    expect(formatDuration(10920000)).toBe("3h 02m");
    expect(formatDuration(273600000)).toBe("3d 4h");
  });
});

describe("formatSpan and formatAge", () => {
  test("formatSpan pads seconds; formatAge keeps one unit", () => {
    expect(formatSpan(421000)).toBe("7m 01s");
    expect(formatSpan(45000)).toBe("45s");
    expect(formatSpan(10920000)).toBe("3h 02m");
    expect(formatAge(421000)).toBe("7m");
    expect(formatAge(7200000)).toBe("2h");
    expect(formatAge(59000)).toBe("59s");
    expect(formatAge(273600000)).toBe("3d");
  });
});
