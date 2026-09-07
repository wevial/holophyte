import { beforeEach, expect, test } from "bun:test";
import { addressOf } from "../src/lib/hosts";
import { tokenedFetch, type Fetch } from "../src/lib/poll";
import { TOKEN_KEY_PREFIX, forgetToken, storeToken, tokenFor, withToken } from "../src/lib/token";

const A = "http://writer:7710";
const B = "http://writer-2:7710";

beforeEach(() => localStorage.clear());

/** A fetch that records each request's URL and headers. */
function recording() {
  const seen: { url: string; headers: Headers }[] = [];
  const fetchImpl: Fetch = async (url, init) => {
    seen.push({ url, headers: new Headers(init?.headers) });
    return Response.json({});
  };
  return { seen, fetchImpl };
}

test("a stored token for A rides every request to A as a bearer header, and B's requests carry none", async () => {
  storeToken(addressOf(A), "s3cret");
  const { seen, fetchImpl } = recording();
  const fetchWithToken = tokenedFetch(fetchImpl);
  await fetchWithToken(`${A}/status`, { headers: { accept: "application/json" } });
  await fetchWithToken(`${B}/status`, { headers: { accept: "application/json" } });
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
