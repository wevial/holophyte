import type { Fetch } from "./poll";

/** The daemon's `GET /config` body (`docs/reference/daemon.md`). */
export interface ConfigBody {
  text: string;
  path: string;
  applies: string;
}

/** What `GET /config` answered: the text, a 401 asking for the token, a
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
    return { state: "ok", config: (await response.json()) as ConfigBody };
  } catch (failure) {
    return { state: "error", error: failure instanceof Error ? failure.message : String(failure) };
  }
}

/** The daemon's verdict on a `PUT /config`: accepted, with the backup it
 *  wrote and when the change applies, or refused with its sentence. A
 *  400 is the loader's refusal (`refused`), naming the file, table and
 *  key; any other failure carries the status and what the daemon said. */
export type ConfigVerdict =
  | { ok: true; backup: string | null; applies: string }
  | { ok: false; error: string; refused: boolean };

/** `PUT /config` with `text` as the whole new file; never throws. */
export async function putConfig(base: string, text: string, fetchImpl: Fetch): Promise<ConfigVerdict> {
  const url = `${base}/config`;
  let response: Response;
  try {
    response = await fetchImpl(url, {
      method: "PUT",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: JSON.stringify({ text }),
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
  if (response.status === 400) return { ok: false, error: error || `${url} answered 400`, refused: true };
  if (!response.ok) return { ok: false, error: `${url} answered ${response.status}${error ? `: ${error}` : ""}`, refused: false };
  return {
    ok: true,
    backup: typeof record.backup === "string" ? record.backup : null,
    applies: typeof record.applies === "string" ? record.applies : "next loop start",
  };
}
