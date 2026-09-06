import { describe, expect, test } from "bun:test";
import { formatDuration } from "../src/lib/format";

describe("formatDuration", () => {
  test("renders the ticket's reference durations", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(754000)).toBe("12m 34s");
    expect(formatDuration(10920000)).toBe("3h 02m");
    expect(formatDuration(273600000)).toBe("3d 4h");
  });
});
