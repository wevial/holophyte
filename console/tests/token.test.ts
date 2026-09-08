import { afterEach, beforeEach, expect, test } from "bun:test";
import { addressOf } from "../src/lib/hosts";
import { defaultPollDeps } from "../src/lib/poll";
import { TOKEN_KEY_PREFIX, checkToken, forgetToken, storeToken, tokenFor, withToken } from "../src/lib/token";

const A = "http://writer:7710";
const B = "http://writer-2:7710";

const realFetch = globalThis.fetch;

beforeEach(() => localStorage.clear());
afterEach(() => {
  globalThis.fetch = realFetch;
});

/** The browser's `Headers` refuses a value outside ISO-8859-1; the test
 *  DOM's does not, so the fake fetch enforces it in its place. */
function latin1Only(headers: Headers) {
  headers.forEach((value, name) => {
    if (/[^\u0000-\u00ff]/.test(value)) throw new TypeError(`Failed to execute 'set' on 'Headers': ${name}`);
  });
  return headers;
}

/** Replace the page's `globalThis.fetch` with one that records each
 *  request's URL and headers, so the production seam is what is tested. */
function recording() {
  const seen: { url: string; headers: Headers }[] = [];
  globalThis.fetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
    seen.push({ url: String(url), headers: latin1Only(new Headers(init?.headers)) });
    return Response.json({});
  }) as typeof fetch;
  return seen;
}

test("a stored token for A rides every request to A as a bearer header through the default fetch seam, and B's requests carry none", async () => {
  storeToken(addressOf(A), "s3cret");
  const seen = recording();
  await defaultPollDeps.fetch(`${A}/status`, { headers: { accept: "application/json" } });
  await defaultPollDeps.fetch(`${B}/status`, { headers: { accept: "application/json" } });
  expect(seen.map(({ url, headers }) => [url, headers.get("authorization")])).toEqual([
    [`${A}/status`, "Bearer s3cret"],
    [`${B}/status`, null],
  ]);
  // The header is added beside the caller's, not in place of them.
  expect(seen[0]!.headers.get("accept")).toBe("application/json");
  expect(seen.every(({ url }) => !url.includes("s3cret"))).toBe(true);
});

test("the token lives under one storage key per address and nowhere else; forgetting it leaves the header off", () => {
  storeToken("writer:7710", "s3cret");
  expect(Object.keys(localStorage)).toEqual([`${TOKEN_KEY_PREFIX}writer:7710`]);
  expect(tokenFor("writer:7710")).toBe("s3cret");
  expect(tokenFor("writer-2:7710")).toBeNull();
  expect(withToken("writer-2:7710", undefined)).toBeUndefined();

  forgetToken("writer:7710");
  expect(tokenFor("writer:7710")).toBeNull();
  expect(localStorage.length).toBe(0);
  expect(new Headers(withToken("writer:7710", { headers: { accept: "*/*" } })?.headers).has("authorization")).toBe(false);

  // Submitting an empty field is a forget, not a stored empty string.
  storeToken("writer:7710", "s3cret");
  storeToken("writer:7710", "");
  expect(localStorage.length).toBe(0);
});

test("the field's check trims a value and refuses a space, a non-breaking space, a character outside printable ASCII, and one over 512 bytes, storing nothing", () => {
  expect(checkToken("  abc123  ")).toBeNull();
  storeToken("writer:7710", "  abc123  ");
  expect(tokenFor("writer:7710")).toBe("abc123");
  localStorage.clear();

  const refused = ["abc 123", "abc\u00a0123", "\u26bfabc", "x".repeat(600)];
  expect(refused.map((value) => checkToken(value))).toEqual([
    "Token has a character the header cannot carry",
    "Token has a character the header cannot carry",
    "Token has a character the header cannot carry",
    "Token is too long",
  ]);
  for (const value of refused) {
    storeToken("writer:7710", value);
    expect(localStorage.length).toBe(0);
  }
});

test("a stored value the header cannot carry never throws from the default fetch seam: the request goes out bare and the value is forgotten", async () => {
  localStorage.setItem(`${TOKEN_KEY_PREFIX}${addressOf(A)}`, "\u26bfabc");
  const seen = recording();
  await expect(defaultPollDeps.fetch(`${A}/status`, { headers: { accept: "application/json" } })).resolves.toBeInstanceOf(Response);
  expect(seen.map(({ url, headers }) => [url, headers.get("authorization")])).toEqual([[`${A}/status`, null]]);
  expect(seen[0]!.headers.get("accept")).toBe("application/json");
  expect(tokenFor(addressOf(A))).toBeNull();
  expect(localStorage.length).toBe(0);
});
