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

/** The longest token the page will keep, in bytes; a daemon's is
 *  `secrets.token_urlsafe` output and far shorter. */
export const TOKEN_MAX_BYTES = 512;

const CANNOT_CARRY = "Token has a character the header cannot carry";
const TOO_LONG = "Token is too long";

/** Why the trimmed `value` cannot be stored, or null when it can. Only
 *  printable ASCII (`0x21` to `0x7E`) is accepted, stricter than the
 *  browser's `Headers` (ISO-8859-1) on purpose: a daemon token never
 *  needs more, and a value the header refuses would fail every request
 *  before it is sent. The reason never repeats the value. */
export function checkToken(value: string): string | null {
  const token = value.trim();
  if (new TextEncoder().encode(token).length > TOKEN_MAX_BYTES) return TOO_LONG;
  if (!/^[\x21-\x7e]*$/.test(token)) return CANNOT_CARRY;
  return null;
}

/** Keep the trimmed `token` for `address`; an empty token is forgotten
 *  instead. Returns the reason when the value is refused and nothing was
 *  stored, null when it was stored (or forgotten). */
export function storeToken(address: string, token: string): string | null {
  const trimmed = token.trim();
  if (trimmed === "") {
    forgetToken(address);
    return null;
  }
  const reason = checkToken(trimmed);
  if (reason != null) return reason;
  try {
    localStorage.setItem(tokenKey(address), trimmed);
  } catch {
    // Storage may be disabled; the daemon will ask again next poll.
  }
  return null;
}

export function forgetToken(address: string): void {
  try {
    localStorage.removeItem(tokenKey(address));
  } catch {
    // Nothing stored, nothing to forget.
  }
}

/** `init` with the bearer header for `address` added when a token is
 *  stored; unchanged otherwise. Headers already in `init` are kept. Never
 *  throws: a stored value the header cannot carry (one that fails
 *  `checkToken`, or that the browser's `Headers` refuses) is forgotten
 *  and the request goes out bare, so the daemon answers 401 and the card
 *  asks again instead of the page reading "unreachable". */
export function withToken(address: string, init?: RequestInit): RequestInit | undefined {
  const token = tokenFor(address);
  if (token == null) return init;
  if (checkToken(token) != null) {
    forgetToken(address);
    return init;
  }
  try {
    const headers = new Headers(init?.headers);
    headers.set("authorization", `Bearer ${token}`);
    return { ...init, headers };
  } catch {
    forgetToken(address);
    return init;
  }
}
