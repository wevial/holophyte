/** One serve token per daemon address, kept in the browser's local
 *  storage and sent as `Authorization: Bearer TOKEN` on every JSON
 *  request to that address (`docs/reference/http.md`, "Authentication").
 *  The value lives here and in the request header only: never in a URL,
 *  never logged, never rendered outside the Hosts card's field. */

export const TOKEN_KEY_PREFIX = "holophyte.token.";

/** The storage key for a daemon's token: the prefix and its `HOST:PORT`. */
export function tokenKey(address: string): string {
  return `${TOKEN_KEY_PREFIX}${address}`;
}

/** The stored token for `address`, null when none is stored or storage
 *  is unavailable. */
export function tokenFor(address: string): string | null {
  try {
    return localStorage.getItem(tokenKey(address));
  } catch {
    return null;
  }
}

/** Keep `token` for `address`; an empty token is forgotten instead. */
export function storeToken(address: string, token: string): void {
  if (token === "") {
    forgetToken(address);
    return;
  }
  try {
    localStorage.setItem(tokenKey(address), token);
  } catch {
    // Storage may be disabled; the daemon will ask again next poll.
  }
}

export function forgetToken(address: string): void {
  try {
    localStorage.removeItem(tokenKey(address));
  } catch {
    // Nothing stored, nothing to forget.
  }
}

/** `init` with the bearer header for `address` added when a token is
 *  stored; unchanged otherwise. Headers already in `init` are kept. */
export function withToken(address: string, init?: RequestInit): RequestInit | undefined {
  const token = tokenFor(address);
  if (token == null) return init;
  const headers = new Headers(init?.headers);
  headers.set("authorization", `Bearer ${token}`);
  return { ...init, headers };
}
