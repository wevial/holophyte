import { describe, expect, test } from "bun:test";

import { APP_SHELL_KEY, TOKEN_KEY_PREFIX, seedScript } from "../seed.ts";

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
    const { value, setCalls } = evaluate(seedScript(tokens, null), store);
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
    const { value, setCalls } = evaluate(seedScript(tokens, null), store);
    expect(setCalls).toBe(0);
    expect(value).toBe(false);
  });

  test("omits a token the page would refuse and keeps the others", () => {
    const script = seedScript({ ...tokens, "100.127.105.41:7712": "héllo" }, null);
    expect(script).not.toContain("100.127.105.41:7712");
    expect(script).not.toContain("héllo");
    expect(script).toContain("100.127.105.41:7710");
    expect(script).toContain("100.127.105.41:7711");
  });

  test("on macOS stores the app-shell flag once, and elsewhere writes no flag", () => {
    const store = new Map<string, string>();
    const first = evaluate(seedScript({}, "macos"), store);
    expect(store.get("holophyte.app-shell")).toBe("macos");
    expect(first.value).toBe(true);
    const second = evaluate(seedScript({}, "macos"), store);
    expect(second).toEqual({ value: false, setCalls: 0 });

    const elsewhere = new Map<string, string>();
    evaluate(seedScript(tokens, null), elsewhere);
    expect(elsewhere.has(APP_SHELL_KEY)).toBe(false);
    expect(elsewhere.size).toBe(2);
  });
});
