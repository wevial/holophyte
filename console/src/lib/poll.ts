import type { z } from "zod";
import { hostStatusSchema, statusSchema } from "./schemas";
import { addressOf, projectBase } from "./hosts";
import { withToken } from "./token";
import type { Attention, HostProject, HostStatus, Status } from "./types";

/** The handoff's cadence: one `/status` + `/attention` round trip every 10 s. */
export const POLL_INTERVAL_MS = 10_000;
/** One tick's requests are abandoned at eight seconds so a slow peer never
 *  overlaps the next tick. */
export const REQUEST_TIMEOUT_MS = 8_000;
/** How often `polledAgo` is refreshed between polls. */
export const TICK_MS = 1_000;

/** Schedule `fn` once after `ms`; returns the cancel. `setTimeout` shaped
 *  so tests can drive time by hand. */
export type Timer = (fn: () => void, ms: number) => () => void;

/** The slice of `fetch` the hook uses; tests hand in a stub. */
export type Fetch = (url: string, init?: RequestInit) => Promise<Response>;

export interface PollDeps {
  fetch: Fetch;
  now: () => number;
  timer: Timer;
}

/** The answers to requests `tokenedFetch` sent with a bearer header. */
const bearerAnswers = new WeakSet<Response>();

/** Whether the request `response` answers carried a bearer token, as it
 *  went out: storage may have changed while it was in flight. */
export function carriedToken(response: Response): boolean {
  return bearerAnswers.has(response);
}

/** `inner` with the stored bearer token for each request's address added
 *  through `withToken`, so every JSON request of the page carries the
 *  token the daemon at that address was given. */
export function tokenedFetch(inner: Fetch): Fetch {
  return async (url, init) => {
    const sent = withToken(addressOf(url), init);
    const response = await inner(url, sent);
    if (new Headers(sent?.headers).has("authorization")) bearerAnswers.add(response);
    return response;
  };
}

export const defaultPollDeps: PollDeps = {
  fetch: tokenedFetch((url, init) => globalThis.fetch(url, init)),
  now: () => Date.now(),
  timer: (fn, ms) => {
    const id = setTimeout(fn, ms);
    return () => clearTimeout(id);
  },
};

export interface PollAnswer {
  status: Status;
  attention: Attention;
}

/** A non-2xx answer, carrying its status so a 401 can be told apart from
 *  a daemon that is down, and whether the request carried a bearer so a
 *  refused token can be told apart from a missing one. */
export class AnswerError extends Error {
  constructor(
    url: string,
    readonly status: number,
    readonly tokenSent = false,
  ) {
    super(`${url} answered ${status}`);
    this.name = "AnswerError";
  }
}

export class ContractError extends Error {
  constructor(readonly endpoint: string, readonly path: string) {
    super(`Response contract mismatch: ${endpoint} at ${path}`);
    this.name = "ContractError";
  }
}

/** One JSON GET; a non-2xx answer throws an `AnswerError` naming the url
 *  and status, and an aborted one throws "timed out" naming the url.
 *  `signal` bounds the wait. */
export async function fetchJson<T>(fetchImpl: Fetch, url: string, schema?: z.ZodType<T>, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetchImpl(url, { headers: { accept: "application/json" }, signal });
  } catch (failure) {
    if (failure instanceof Error && (failure.name === "TimeoutError" || failure.name === "AbortError")) {
      throw new Error(`${url} timed out`);
    }
    throw failure;
  }
  if (!response.ok) throw new AnswerError(url, response.status, carriedToken(response));
  const body: unknown = await response.json();
  // Endpoints outside the status/run-detail contract retain their existing types.
  return schema ? parseAt(schema, body, url) : (body as T);
}

/** `body` checked against `schema`, a `ContractError` naming `url` and the
 *  first mismatched path when it does not fit. */
function parseAt<T>(schema: z.ZodType<T>, body: unknown, url: string): T {
  const parsed = schema.safeParse(body);
  if (!parsed.success) throw new ContractError(url, parsed.error.issues[0]!.path.join(".") || "$");
  return parsed.data;
}

/** One round trip: both endpoints of the daemon at `base`, both bounded by
 *  `signal` when given. */
export async function pollOnce(base: string, fetchImpl: Fetch, signal?: AbortSignal): Promise<PollAnswer> {
  const [status, attention] = await Promise.all([
    fetchJson(fetchImpl, `${base}/status`, statusSchema, signal),
    fetchJson<Attention>(fetchImpl, `${base}/attention`, undefined, signal),
  ]);
  return { status, attention };
}

/** A failed request as a poll result's fields: the message, and for an
 *  answer other than 2xx its status and whether it carried a bearer; a
 *  body that broke the contract is marked so. */
export interface PollFailure {
  ok: false;
  error: string;
  status?: number;
  contract_error?: boolean;
  /** The request that failed carried a saved bearer token. */
  token_sent?: boolean;
}

export function pollFailure(failure: unknown): PollFailure {
  const error = failure instanceof Error ? failure.message : String(failure);
  if (failure instanceof AnswerError) return { ok: false, error, status: failure.status, token_sent: failure.tokenSent };
  return { ok: false, error, contract_error: failure instanceof ContractError };
}

/** One project behind a host daemon: its name and path from the root's
 *  list, and its own `/status` and `/attention` from under its prefix, or
 *  why they could not be had. */
export type ProjectAnswer = { name: string; path: string } & ({ ok: true; status: Status; attention: Attention } | PollFailure);

/** What one daemon answered: a project daemon's two bodies at its root,
 *  or a host daemon's root `/status` and `/attention` with each project's
 *  own answers. */
export type DaemonAnswer =
  | ({ kind: "project" } & PollAnswer)
  | { kind: "host"; host: HostStatus; attention: Attention; projects: ProjectAnswer[] };

/** Whether a root `/status` body is a host daemon's: a `projects` list
 *  where a project daemon names its one `project`. */
export function isHostBody(body: unknown): boolean {
  return typeof body === "object" && body != null && Array.isArray((body as { projects?: unknown }).projects)
    && typeof (body as { project?: unknown }).project !== "string";
}

/** Why the root says a project cannot be asked under its prefix, or null
 *  when it can: its own error, no store yet, or no row for its path (the
 *  prefix's `/status` is 503 for each). */
export function unaskable(project: HostProject): string | null {
  if (project.error != null) return project.error;
  if (project.store == null) return `${project.path} has no store; \`factory.py project add ${project.path}\` creates it`;
  if (project.project_row == null) return `the store has no project row for ${project.path}; \`factory.py project add ${project.path}\` writes it`;
  return null;
}

/** One project of a host daemon: `/status` and `/attention` under
 *  `/projects/NAME`, unless the root already said why not. */
async function pollProject(root: string, project: HostProject & { name: string }, fetchImpl: Fetch, signal?: AbortSignal): Promise<ProjectAnswer> {
  const { name, path } = project;
  const why = unaskable(project);
  if (why != null) return { name, path, ok: false, error: why };
  try {
    return { name, path, ok: true, ...(await pollOnce(projectBase(root, name), fetchImpl, signal)) };
  } catch (failure) {
    return { name, path, ...pollFailure(failure) };
  }
}

/** One round trip to the daemon at `base`: its root `/status` and
 *  `/attention`; when the status is a host's, each registered project's
 *  own two under its prefix, side by side. A project the registry cannot
 *  name (its config does not load) is listed by the root and not asked.
 *  Every request is bounded by `signal` when given. */
export async function pollDaemon(base: string, fetchImpl: Fetch, signal?: AbortSignal): Promise<DaemonAnswer> {
  const url = `${base}/status`;
  const [body, attention] = await Promise.all([
    fetchJson<unknown>(fetchImpl, url, undefined, signal),
    fetchJson<Attention>(fetchImpl, `${base}/attention`, undefined, signal),
  ]);
  if (!isHostBody(body)) return { kind: "project", status: parseAt(statusSchema, body, url), attention };
  const host = parseAt(hostStatusSchema, body, url);
  const named = host.projects.filter((project): project is HostProject & { name: string } => project.name != null);
  const projects = await Promise.all(named.map((project) => pollProject(base, project, fetchImpl, signal)));
  return { kind: "host", host, attention, projects };
}
