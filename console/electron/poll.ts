/**
 * One poll of every daemon the console names: `/peers` on the configured
 * console URL, then `/status` and `/attention` on each address it lists
 * (and `/runs` on the idle ones, for the last-merge line). Each request
 * carries the bearer `console.json` holds for that address and gives up
 * after `timeoutMs`. No Electron import: `main.ts` supplies the config
 * text, the user-data directory and the clock; a test supplies a fake
 * `fetch`.
 *
 * Tokens come from `console.json` as `tokens: { "HOST:PORT": "..." }` or
 * as `token_files: { "HOST:PORT": "/path" }`, the latter the same files
 * the drawer's `[[daemon]] token_file` names and the daemon's `[serve]
 * token_file` holds, read once per poll and never shown. The tray never
 * prompts: a daemon whose token is missing is a "needs token" line, and
 * one that refuses a token read from a file names that file as out of
 * date, until the file is fixed.
 */
import { readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";

import type { Attention, FetchResult, Runs, Status } from "./tray.ts";

export const TIMEOUT_MS = 2000;
export const POLL_INTERVAL_MS = 10_000;

export type PeersBody = { self?: string; peers?: string[]; daemons?: string[] };

export type PollAnswer = {
  peers: string[];
  statuses: Record<string, FetchResult<Status>>;
  attentions: Record<string, FetchResult<Attention>>;
  runs: Record<string, FetchResult<Runs>>;
  /** The `token_files` path whose token each address was sent, for the
   *  addresses whose bearer came from a file. */
  tokenFiles: Record<string, string>;
};

type FetchLike = (url: string, init: { headers: Record<string, string>; signal: AbortSignal }) => Promise<Response>;

export type PollDeps = {
  fetch?: FetchLike;
  timeoutMs?: number;
  /** Reads a token file; the default is the file system. */
  readFile?: (file: string) => string;
  /** Where each bearer in `tokens` came from, as `readTokenSources`
   *  returns it: an address sent a token from one of these files is
   *  recorded in the answer's `tokenFiles`. */
  tokenFiles?: Record<string, string>;
};

/** The bearers per address and, for those read from `token_files`, the
 *  resolved path each came from. */
export type TokenSources = { tokens: Record<string, string>; files: Record<string, string> };

/** The bearer per address from `console.json`'s text. `tokens` wins over
 *  `token_files` for the same address; a path in `token_files` may start
 *  with `~` or be relative to `baseDir` (the directory the config lives
 *  in). A file that cannot be read is treated as no token, so the daemon
 *  shows "needs token" rather than the tray failing to build. */
export function readTokens(
  fileText: string | null,
  baseDir: string,
  readFile: (file: string) => string = (file) => readFileSync(file, "utf8"),
): Record<string, string> {
  return readTokenSources(fileText, baseDir, readFile).tokens;
}

/** `readTokens`, with the path each file-read token came from beside it,
 *  so a 401 can name the file to replace. An address whose inline token
 *  wins has no file. */
export function readTokenSources(
  fileText: string | null,
  baseDir: string,
  readFile: (file: string) => string = (file) => readFileSync(file, "utf8"),
): TokenSources {
  const out: TokenSources = { tokens: {}, files: {} };
  if (fileText === null) return out;
  let data: unknown;
  try {
    data = JSON.parse(fileText);
  } catch {
    return out;
  }
  if (data === null || typeof data !== "object") return out;
  const { tokens, token_files } = data as { tokens?: unknown; token_files?: unknown };
  if (token_files !== null && typeof token_files === "object") {
    for (const [address, file] of Object.entries(token_files as Record<string, unknown>)) {
      if (typeof file !== "string") continue;
      const resolved = expandPath(file, baseDir);
      try {
        out.tokens[address] = readFile(resolved).trim();
        out.files[address] = resolved;
      } catch {
        // No token for this address; its line says so.
      }
    }
  }
  if (tokens !== null && typeof tokens === "object") {
    for (const [address, token] of Object.entries(tokens as Record<string, unknown>)) {
      if (typeof token !== "string") continue;
      out.tokens[address] = token;
      delete out.files[address];
    }
  }
  return out;
}

function expandPath(file: string, baseDir: string): string {
  const expanded = file === "~" || file.startsWith("~/") ? path.join(os.homedir(), file.slice(1)) : file;
  return path.isAbsolute(expanded) ? expanded : path.resolve(baseDir, expanded);
}

/** `HOST:PORT` of a base URL. */
export function addressOf(base: string): string {
  try {
    return new URL(base).host;
  } catch {
    return base;
  }
}

/** The endpoint base for a `/peers` address: `http://HOST:PORT`, or the
 *  address itself when it already names a scheme. */
export function baseOf(address: string): string {
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(address) ? address.replace(/\/+$/, "") : `http://${address}`;
}

/** One GET, parsed. A 401 is `unauthorized`; another non-2xx keeps its
 *  status and JSON body when there is one; a network error or a timeout is
 *  `unreachable` with the error's text. */
export async function fetchJson<T>(
  fetchImpl: FetchLike,
  url: string,
  token: string | undefined,
  timeoutMs: number,
): Promise<FetchResult<T>> {
  const headers: Record<string, string> = { accept: "application/json" };
  if (token !== undefined) headers.authorization = `Bearer ${token}`;
  try {
    const response = await fetchImpl(url, { headers, signal: AbortSignal.timeout(timeoutMs) });
    if (response.status === 401) return { ok: false, kind: "unauthorized" };
    let body: unknown;
    try {
      body = await response.json();
    } catch (err) {
      if (response.ok) return { ok: false, kind: "unreachable", error: `not JSON: ${message(err)}` };
    }
    if (!response.ok) return { ok: false, kind: "http", status: response.status, body };
    return { ok: true, body: body as T };
  } catch (err) {
    return { ok: false, kind: "unreachable", error: message(err) };
  }
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/** The addresses to poll: the console's own first, then each peer once. */
export function peerAddresses(consoleUrl: string, peers: FetchResult<PeersBody>): { address: string; base: string }[] {
  const origin = new URL(consoleUrl).origin;
  const self = (peers.ok ? peers.body.self : undefined) ?? addressOf(origin);
  const seen = new Set([self]);
  const out = [{ address: self, base: origin }];
  const listed = peers.ok ? (peers.body.peers ?? peers.body.daemons ?? []) : [];
  for (const address of listed) {
    if (seen.has(address)) continue;
    seen.add(address);
    out.push({ address, base: baseOf(address) });
  }
  return out;
}

/** One poll over every daemon. A daemon that did not answer `/status` is
 *  not asked again, so a dead host costs one timeout, not three. */
export async function pollAll(
  consoleUrl: string,
  tokens: Record<string, string>,
  deps: PollDeps = {},
): Promise<PollAnswer> {
  const fetchImpl: FetchLike = deps.fetch ?? ((url, init) => fetch(url, init));
  const timeoutMs = deps.timeoutMs ?? TIMEOUT_MS;
  const origin = new URL(consoleUrl).origin;
  const peers = await fetchJson<PeersBody>(fetchImpl, `${origin}/peers`, undefined, timeoutMs);
  const daemons = peerAddresses(consoleUrl, peers);
  const answer: PollAnswer = { peers: daemons.map((d) => d.address), statuses: {}, attentions: {}, runs: {}, tokenFiles: {} };
  await Promise.all(
    daemons.map(async ({ address, base }) => {
      const key = tokens[address] !== undefined ? address : addressOf(base);
      const token = tokens[key];
      const file = token === undefined ? undefined : deps.tokenFiles?.[key];
      if (file !== undefined) answer.tokenFiles[address] = file;
      const get = <T>(p: string) => fetchJson<T>(fetchImpl, `${base}${p}`, token, timeoutMs);
      const status = await get<Status>("/status");
      answer.statuses[address] = status;
      if (!status.ok) return;
      const [attention, runs] = await Promise.all([
        get<Attention>("/attention"),
        status.body.runs?.length === 0 ? get<Runs>("/runs") : Promise.resolve(undefined),
      ]);
      answer.attentions[address] = attention;
      if (runs !== undefined) answer.runs[address] = runs;
    }),
  );
  return answer;
}
