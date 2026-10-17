import type { Fetch } from "./poll";

export const CONFIG_EDIT_OFF = "Read-only: this daemon has not opted into config edits ([serve] config_edit = true)";

/** A value the daemon's `PUT /config` patch carries for one dotted key:
 *  a string, an integer, a boolean or a list of strings
 *  (`holophyte/serve/serve_config.py` `check_patch_value()`). */
export type PatchValue = string | number | boolean | string[];

/** The parsed configuration the daemon serves beside the text: every
 *  `[table]` as an object of its keys, as `tomllib` read the redacted
 *  text; null when the text does not parse. The console reads settings
 *  here and never parses TOML itself. */
export type ConfigValues = Record<string, Record<string, unknown>>;

export const UNBOUND_NOTE = "edit this one in Raw TOML";

export type Bound = { state: "absent" } | { state: "bound"; value: PatchValue } | { state: "unbound"; value: unknown };

export const isLines = (value: unknown): value is string[] => Array.isArray(value) && value.every((item) => typeof item === "string");

export function bind(field: { table: string; key: string; kind: string; numeric?: string; min?: number }, values: ConfigValues | null): Bound {
  const table = values?.[field.table];
  if (table == null || typeof table !== "object" || !(field.key in table)) return { state: "absent" };
  const value: unknown = table[field.key];
  if (typeof value === "number" && ((field.numeric === "integer" && !Number.isInteger(value)) || (field.min !== undefined && value < field.min))) return { state: "unbound", value };
  const fits =
    field.kind === "number" ? typeof value === "number" && Number.isFinite(value) : field.kind === "lines" ? isLines(value) : field.kind === "boolean" ? typeof value === "boolean" : typeof value === "string";
  return fits ? { state: "bound", value: value as PatchValue } : { state: "unbound", value };
}

export const same = (a: PatchValue, b: PatchValue) => (Array.isArray(a) && Array.isArray(b) ? a.length === b.length && a.every((item, at) => item === b[at]) : a === b);

/** The daemon's `GET /config` body (`docs/reference/daemon.md`). */
export interface ConfigBody {
  text: string;
  values: ConfigValues | null;
  path: string;
  applies: string;
  revision: string;
  saved: { at_ms: number; actor: "maintainer" | "operator" | "supervisor" | null } | null;
  modified_ms: number | null;
}

/** What `GET /config` answered: the body, a 401 asking for the token, a
 *  404 for a daemon without `[serve] config_edit`, or a failure. */
export type ConfigAnswer =
  | { state: "ok"; config: ConfigBody }
  | { state: "needs_token" }
  | { state: "off" }
  | { state: "error"; error: string };

/** One `GET /config` from `base`; never throws. The bearer rides in
 *  through the page's fetch seam (`tokenedFetch`). */
export async function fetchConfig(base: string, fetchImpl: Fetch): Promise<ConfigAnswer> {
  const url = `${base}/config`;
  try {
    const response = await fetchImpl(url, { headers: { accept: "application/json" } });
    if (response.status === 401) return { state: "needs_token" };
    if (response.status === 404) return { state: "off" };
    if (!response.ok) return { state: "error", error: `${url} answered ${response.status}` };
    const body = (await response.json()) as Partial<ConfigBody>;
    return {
      state: "ok",
      config: {
        text: typeof body.text === "string" ? body.text : "",
        values: isValues(body.values) ? body.values : null,
        path: typeof body.path === "string" ? body.path : "",
        applies: typeof body.applies === "string" ? body.applies : "next loop start",
        revision: typeof body.revision === "string" ? body.revision : "",
        saved: body.saved ?? null,
        modified_ms: typeof body.modified_ms === "number" ? body.modified_ms : null,
      },
    };
  } catch (failure) {
    return { state: "error", error: failure instanceof Error ? failure.message : String(failure) };
  }
}

const isValues = (candidate: unknown): candidate is ConfigValues =>
  typeof candidate === "object" && candidate != null && !Array.isArray(candidate);

export type ConfigProbe = { ok: boolean; output: string[] };

/** The daemon's verdict on a `PUT /config`: accepted, with the backup it
 *  wrote and when the change applies, or refused with its sentence. A
 *  400 is the daemon's refusal (`refused`) -- the loader's, naming the
 *  file, table and key, or the patch's, naming the dotted key. A revision
 *  conflict carries the current revision; other failures carry the status. */
export type ConfigVerdict =
  | { ok: true; backup: string | null; applies: string; revision: string; probe?: ConfigProbe | null }
  | { ok: false; conflict: true; current: string }
  | { ok: false; error: string; refused: true; line: number | null }
  | { ok: false; error: string; refused: false };

export type ConfigCheck =
  | { ok: true }
  | { ok: false; error: string; line: number | null }
  | { ok: false; error: string };

export async function checkConfig(base: string, text: string, fetchImpl: Fetch): Promise<ConfigCheck> {
  const url = `${base}/config`;
  try {
    const response = await fetchImpl(url, {
      method: "PUT",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ text, check: true }),
    });
    let answer: unknown = null;
    try {
      answer = await response.json();
    } catch {
      answer = null;
    }
    const record = typeof answer === "object" && answer != null ? (answer as Record<string, unknown>) : {};
    const error = typeof record.error === "string" ? record.error : "";
    if (response.status === 400) return { ok: false, error: error || `${url} answered 400`, line: typeof record.line === "number" ? record.line : null };
    if (!response.ok) return { ok: false, error: `${url} answered ${response.status}${error ? `: ${error}` : ""}` };
    return { ok: true };
  } catch (failure) {
    return { ok: false, error: failure instanceof Error ? failure.message : String(failure) };
  }
}

/** What a `PUT /config` carries: the whole new file as `text`, or a
 *  `patch` of dotted `table.key` to the value each takes. */
export type ConfigWrite = { text: string } | { patch: Record<string, PatchValue> };

/** `PUT /config` with `write` as its body; never throws. */
export function putConfig(base: string, write: ConfigWrite, fetchImpl: Fetch): Promise<Exclude<ConfigVerdict, { conflict: true }>>;
export function putConfig(base: string, write: ConfigWrite, fetchImpl: Fetch, revision: string | undefined): Promise<ConfigVerdict>;
export async function putConfig(base: string, write: ConfigWrite, fetchImpl: Fetch, revision?: string): Promise<ConfigVerdict> {
  const url = `${base}/config`;
  let response: Response;
  try {
    response = await fetchImpl(url, {
      method: "PUT",
      headers: { "content-type": "application/json", accept: "application/json", ...(revision === undefined ? {} : { "If-Match": revision }) },
      body: JSON.stringify(write),
    });
  } catch (failure) {
    return { ok: false, error: failure instanceof Error ? failure.message : String(failure), refused: false };
  }
  let answer: unknown = null;
  try {
    answer = await response.json();
  } catch {
    answer = null;
  }
  const record = typeof answer === "object" && answer != null ? (answer as Record<string, unknown>) : {};
  const error = typeof record.error === "string" ? record.error : "";
  if (response.status === 409 && revision !== undefined) return { ok: false, conflict: true, current: typeof record.current === "string" ? record.current : "" };
  if (response.status === 400) return { ok: false, error: error || `${url} answered 400`, refused: true, line: typeof record.line === "number" ? record.line : null };
  if (!response.ok) return { ok: false, error: `${url} answered ${response.status}${error ? `: ${error}` : ""}`, refused: false };
  const probe = record.probe as Partial<ConfigProbe> | null | undefined;
  return {
    ok: true,
    ...(probe && typeof probe.ok === "boolean" && isLines(probe.output) ? { probe: { ok: probe.ok, output: probe.output } } : {}),
    backup: typeof record.backup === "string" ? record.backup : null,
    applies: typeof record.applies === "string" ? record.applies : "next loop start",
    revision: typeof record.revision === "string" ? record.revision : "",
  };
}

/** The `table.key` a daemon's refusal names, if it names one the way
 *  the loader writes them (`[loop] workers must be ...`) or the patch
 *  does (`loop.workers: a patch value is ...`); null otherwise, so the
 *  sheet shows the sentence under the raw tab instead. */
export function namedKey(message: string): string | null {
  const loader = /\[([A-Za-z0-9_-]+)\]\s+([A-Za-z0-9_-]+)\b/.exec(message);
  if (loader) return `${loader[1]}.${loader[2]}`;
  const patch = /^([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+):\s/.exec(message);
  return patch ? patch[1]! : null;
}
