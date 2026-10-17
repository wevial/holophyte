import { expect, test } from "bun:test";
import { fetchConfig, putConfig } from "../src/lib/config";

test("putConfig sends the revision and reports a conflicting current revision", async () => {
  const verdict = await putConfig("http://daemon", { patch: { "loop.workers": 3 } }, async (_url, init) => {
    expect(new Headers(init?.headers).get("If-Match")).toBe("def");
    return Response.json({ ok: false, current: "abc" }, { status: 409 });
  }, "def");
  expect(verdict).toEqual({ ok: false, conflict: true, current: "abc" });
});

test("fetchConfig preserves the file revision and last save metadata", async () => {
  const answer = await fetchConfig("http://daemon", async () => Response.json({
    text: "[loop]\nworkers = 2\n", values: { loop: { workers: 2 } }, path: "/project/config.toml",
    applies: "next loop start", revision: "abc", modified_ms: 1234,
    saved: { at_ms: 1200, actor: "maintainer" },
  }));
  expect(answer).toMatchObject({ state: "ok", config: {
    revision: "abc", modified_ms: 1234, saved: { at_ms: 1200, actor: "maintainer" },
  } });
});

test("putConfig without a revision omits If-Match and returns the saved revision", async () => {
  const verdict = await putConfig("http://daemon", { text: "[loop]\nworkers = 3\n" }, async (_url, init) => {
    expect(new Headers(init?.headers).has("If-Match")).toBe(false);
    return Response.json({ ok: true, backup: null, applies: "next loop start", revision: "abc" });
  });
  expect(verdict).toEqual({ ok: true, backup: null, applies: "next loop start", revision: "abc" });
});
