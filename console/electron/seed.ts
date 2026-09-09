/**
 * The script the window runs in the console page once it has loaded, so
 * the page holds the same bearers the tray reads from `console.json`
 * before its first poll. Pure: `main.ts` hands the text to
 * `webContents.executeJavaScript`, which runs it in the page's origin so
 * the keys land in the storage the React page reads. The script
 * evaluates to `true` when it changed at least one key, which is the
 * window's cue to reload once; a second run finds nothing to change.
 *
 * The page's rules are restated here rather than imported, so the
 * Electron package does not build against the renderer: the key is
 * `TOKEN_KEY_PREFIX` plus the `HOST:PORT` address and a token is
 * accepted when `checkToken` would accept it (trimmed, printable ASCII
 * `0x21` to `0x7E`, at most `TOKEN_MAX_BYTES` bytes), both from
 * `console/src/lib/token.ts`. Tokens are embedded with `JSON.stringify`
 * and never leave the script text.
 */

export const TOKEN_KEY_PREFIX = "holophyte.token.";
export const TOKEN_MAX_BYTES = 512;

/** Whether the page's `checkToken` would accept `token` (trimmed). */
export function acceptable(token: string): boolean {
  const trimmed = token.trim();
  if (trimmed === "") return false;
  if (new TextEncoder().encode(trimmed).length > TOKEN_MAX_BYTES) return false;
  return /^[\x21-\x7e]*$/.test(trimmed);
}

/** JavaScript text that stores each acceptable token under the page's
 *  key, skipping a key whose value is already the same, and evaluates
 *  to `true` when it wrote at least one key, `false` otherwise. */
export function seedScript(tokens: Record<string, string>): string {
  const entries: [string, string][] = [];
  for (const [address, token] of Object.entries(tokens)) {
    if (!acceptable(token)) continue;
    entries.push([`${TOKEN_KEY_PREFIX}${address}`, token.trim()]);
  }
  return [
    "(() => {",
    `  const entries = ${JSON.stringify(entries)};`,
    "  let changed = false;",
    "  for (const [key, value] of entries) {",
    "    if (localStorage.getItem(key) === value) continue;",
    "    localStorage.setItem(key, value);",
    "    changed = true;",
    "  }",
    "  return changed;",
    "})()",
  ].join("\n");
}
