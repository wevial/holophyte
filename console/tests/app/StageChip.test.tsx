import { expect, test } from "bun:test";
import { roundLabel } from "../../src/app/components/StageChip";

test("a round names its cap only while it is within it", () => {
  expect(roundLabel(3, 2)).toBe("round 3");
  expect(roundLabel(2, 2)).toBe("round 2 of 2");
  expect(roundLabel(2, undefined)).toBe("round 2");
});
