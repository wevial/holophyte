import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import { LOG_FILE, LOG_LIMIT_BYTES, appendLog, consoleLine, failedLoadLine } from "../log.ts";

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "holophyte-log-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z /;

describe("appendLog", () => {
  test("two calls leave two timestamped lines in console.log, in order", () => {
    appendLog(dir, "first");
    appendLog(dir, "second");
    const lines = readFileSync(path.join(dir, LOG_FILE), "utf8").split("\n");
    expect(lines).toHaveLength(3);
    expect(lines[2]).toBe("");
    expect(lines[0]).toMatch(ISO);
    expect(lines[1]).toMatch(ISO);
    expect(lines[0]?.endsWith(" first")).toBe(true);
    expect(lines[1]?.endsWith(" second")).toBe(true);
  });

  test("a file past the limit is truncated before the next line is appended", () => {
    const file = path.join(dir, LOG_FILE);
    writeFileSync(file, "x".repeat(LOG_LIMIT_BYTES + 1));
    appendLog(dir, "after");
    const text = readFileSync(file, "utf8");
    expect(statSync(file).size).toBeLessThan(200);
    expect(text.split("\n")).toHaveLength(2);
    expect(text).toMatch(ISO);
    expect(text.endsWith(" after\n")).toBe(true);
  });
});

describe("consoleLine", () => {
  test("carries the message and the source path, never the query string", () => {
    const line = consoleLine("Failed to fetch", "http://writer:7710/runs?token=SECRET#frag", 12);
    expect(line).toContain("Failed to fetch");
    expect(line).toContain("/runs");
    expect(line).not.toContain("SECRET");
    expect(line).not.toContain("token");
    expect(line).not.toContain("?");
  });

  test("a URL inside the message text loses its query string and fragment too", () => {
    const line = consoleLine(
      "Request failed: https://writer:7710/runs?token=SECRET#frag then http://writer:7710/api/x?t=SECRET2 done",
      "http://writer:7710/console.js",
      3,
    );
    expect(line).toContain("Request failed: https://writer:7710/runs then http://writer:7710/api/x done");
    expect(line).not.toContain("SECRET");
    expect(line).not.toContain("?");
    expect(line).not.toContain("#");
  });

  test("a query string containing brackets or quotes is dropped whole", () => {
    const line = consoleLine(
      "Request failed: https://writer:7710/runs?filter=(active)&token=SECRET and https://writer:7710/x?a=[1]&t=SECRET2 'https://writer:7710/y?q=\"z\"&k=SECRET3'",
      "http://writer:7710/console.js",
      3,
    );
    expect(line).toContain("https://writer:7710/runs and https://writer:7710/x");
    expect(line).toContain("https://writer:7710/y");
    expect(line).not.toContain("SECRET");
    expect(line).not.toContain("token");
    expect(line).not.toContain("?");
  });

  test("a path containing parentheses still loses its query string", () => {
    const line = consoleLine("Request failed: https://writer:7710/runs(active)?token=SECRET", "", 0);
    expect(line).toContain("https://writer:7710/runs(active)");
    expect(line).not.toContain("SECRET");
    expect(line).not.toContain("?");
  });

  test("a source that is not a URL is kept as-is", () => {
    expect(consoleLine("boom", "", 0)).toContain("boom");
  });
});

describe("failedLoadLine", () => {
  test("names the error and the path, never the query string", () => {
    const line = failedLoadLine(-106, "ERR_INTERNET_DISCONNECTED", "http://writer:7710/?t=SECRET");
    expect(line).toContain("ERR_INTERNET_DISCONNECTED");
    expect(line).toContain("-106");
    expect(line).not.toContain("SECRET");
  });
});
