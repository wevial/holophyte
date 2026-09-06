import { expect, test } from "bun:test";
import { isStale, isSupervisorStale, projectName } from "../src/lib/derive";

test("projectName is the last path segment, trailing slash ignored", () => {
  expect(projectName("/srv/dev/writer")).toBe("writer");
  expect(projectName("/srv/dev/writer/")).toBe("writer");
  expect(projectName("writer")).toBe("writer");
});

test("a run is stale strictly past the threshold, the supervisor at it", () => {
  expect(isStale(180000, 180000)).toBe(false);
  expect(isStale(180001, 180000)).toBe(true);
  const at = { state: "live" as const, pid: 1, heartbeat_age_ms: 180000, host: "writer" };
  expect(isSupervisorStale(at, 180000)).toBe(true);
  expect(isSupervisorStale({ ...at, heartbeat_age_ms: 179999 }, 180000)).toBe(false);
});
