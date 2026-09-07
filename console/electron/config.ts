/**
 * Where the desktop wrapper finds the console. Pure: no Electron, no I/O.
 *
 * Precedence: `HOLOPHYTE_CONSOLE_URL`, then `console.json` in Electron's
 * user-data directory (`{"url": "…"}`), then the default for the first
 * target on a host. Whichever source speaks first is final — a bad value
 * is reported as an error naming that source, never skipped for the next.
 */

export const DEFAULT_CONSOLE_URL = "http://127.0.0.1:7710/";
export const ENV_VAR = "HOLOPHYTE_CONSOLE_URL";
export const CONFIG_FILE = "console.json";

export type UrlSource = typeof ENV_VAR | typeof CONFIG_FILE | "default";

export type ResolvedUrl =
  | { url: string; source: UrlSource }
  | { error: string; source: UrlSource };

const ALLOWED_SCHEMES = new Set(["http:", "https:"]);

function checkScheme(candidate: string, source: UrlSource): ResolvedUrl {
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return { error: `${source}: ${JSON.stringify(candidate)} is not a URL`, source };
  }
  if (!ALLOWED_SCHEMES.has(parsed.protocol)) {
    return {
      error: `${source}: scheme must be http or https, got ${parsed.protocol}`,
      source,
    };
  }
  return { url: parsed.href, source };
}

function fromFile(text: string): ResolvedUrl {
  const source = CONFIG_FILE;
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err);
    return { error: `${source}: invalid JSON (${detail})`, source };
  }
  const url =
    data !== null && typeof data === "object" ? (data as { url?: unknown }).url : undefined;
  if (typeof url !== "string") {
    return { error: `${source}: "url" must be a string`, source };
  }
  return checkScheme(url, source);
}

/**
 * `env` is the process environment (or a stand-in); `fileText` is the
 * contents of `console.json`, or null when the file does not exist.
 */
export function resolveConsoleUrl(
  env: Record<string, string | undefined>,
  fileText: string | null,
): ResolvedUrl {
  const fromEnv = env[ENV_VAR];
  if (fromEnv !== undefined) {
    return checkScheme(fromEnv, ENV_VAR);
  }
  if (fileText !== null) {
    return fromFile(fileText);
  }
  return { url: DEFAULT_CONSOLE_URL, source: "default" };
}
