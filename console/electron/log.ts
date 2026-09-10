/**
 * The window's error log: one timestamped line per console error or failed
 * load the page reports, appended to `console.log` in the user data
 * directory and truncated once the file passes LOG_LIMIT_BYTES. Pure
 * `node:fs`, no Electron import, so the rule is testable under `bun test`.
 * A line carries URL paths only — the source URL's and any URL quoted in
 * the message text — never a query string or fragment, where a bearer
 * token could travel.
 */
import { appendFileSync, statSync, truncateSync } from "node:fs";
import path from "node:path";

export const LOG_FILE = "console.log";
export const LOG_LIMIT_BYTES = 1024 * 1024;

/** Appends `line` with a UTC timestamp, truncating first when the file has passed the limit. */
export function appendLog(userData: string, line: string, now: Date = new Date()): void {
  const file = path.join(userData, LOG_FILE);
  try {
    if (statSync(file).size > LOG_LIMIT_BYTES) truncateSync(file, 0);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== "ENOENT") throw err;
  }
  appendFileSync(file, `${now.toISOString()} ${line.replace(/\r?\n/g, " ")}\n`);
}

/** The path of a source URL, with query string and fragment dropped; a non-URL source as given. */
export function sourcePath(source: string): string {
  try {
    return new URL(source).pathname;
  } catch {
    return source.split(/[?#]/, 1)[0] ?? source;
  }
}

/**
 * Strips the query string and fragment from every URL quoted in free text.
 * The path runs up to the first `?` or `#`, stopping only at whitespace or a
 * quote, so a path such as `/runs(active)` is not cut short by a bracket.
 * Once the query starts, everything up to the next whitespace goes with it —
 * brackets and quotes included — so a value such as `filter=(active)&token=...`
 * cannot split the match and leak its tail.
 */
export function redactUrls(text: string): string {
  return text.replace(/\bhttps?:\/\/[^\s"'<>?#]*[?#]\S*/g, (url) => url.split(/[?#]/, 1)[0] ?? url);
}

export function consoleLine(message: string, source: string, lineNumber: number): string {
  return `console.error ${redactUrls(message)} (${sourcePath(source)}:${lineNumber})`;
}

export function failedLoadLine(errorCode: number, errorDescription: string, url: string): string {
  return `did-fail-load ${errorCode} ${errorDescription} (${sourcePath(url)})`;
}
