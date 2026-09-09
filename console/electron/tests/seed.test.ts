import { describe, expect, test } from "bun:test";

import { TOKEN_KEY_PREFIX, seedScript } from "../seed.ts";

/** Evaluates the script text against a fake `localStorage`, returning the
 *  script's value and the calls it made. */
function evaluate(script: string, store: Map<string, string>): { value: unknown; setCalls: number } {
  let setCalls = 0;
  const localStorage = {
    getItem: (key: string) => (store.has(key) ? (store.get(key) as string) : null),
    setItem: (key: string, value: string) => {
      setCalls += 1;
      store.set(key, value);
    },
  };
  const value = new Function("localStorage", `return ${script};`)(localStorage);
  return { value, setCalls };
}

describe("seedScript", () => {
  const tokens = { "100.127.105.41:7710": "abc", "100.127.105.41:7711": "def" };

  test("stores every token under the page's key and reports a change", () => {
    const store = new Map<string, string>();
    const { value, setCalls } = evaluate(seedScript(tokens), store);
    expect(store.get(`${TOKEN_KEY_PREFIX}100.127.105.41:7710`)).toBe("abc");
    expect(store.get(`${TOKEN_KEY_PREFIX}100.127.105.41:7711`)).toBe("def");
    expect(setCalls).toBe(2);
    expect(value).toBe(true);
  });

  test("leaves stored tokens alone and reports no change", () => {
    const store = new Map<string, string>([
      [`${TOKEN_KEY_PREFIX}100.127.105.41:7710`, "abc"],
      [`${TOKEN_KEY_PREFIX}100.127.105.41:7711`, "def"],
    ]);
    const { value, setCalls } = evaluate(seedScript(tokens), store);
    expect(setCalls).toBe(0);
    expect(value).toBe(false);
  });

  test("omits a token the page would refuse and keeps the others", () => {
    const script = seedScript({ ...tokens, "100.127.105.41:7712": "héllo" });
    expect(script).not.toContain("100.127.105.41:7712");
    expect(script).not.toContain("héllo");
    expect(script).toContain("100.127.105.41:7710");
    expect(script).toContain("100.127.105.41:7711");
  });
});
