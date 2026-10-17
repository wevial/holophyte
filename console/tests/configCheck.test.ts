import { expect, test } from "bun:test";
import { checkConfig, putConfig } from "../src/lib/config";

test("checkConfig requests a text check and preserves the refused line", async () => {
  const verdict = await checkConfig("http://daemon", "[loop]\nworkers = 0\n", async (url, init) => {
    expect(url).toBe("http://daemon/config");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({ text: "[loop]\nworkers = 0\n", check: true });
    return Response.json({ error: "e", line: 5 }, { status: 400 });
  });
  expect(verdict).toEqual({ ok: false, error: "e", line: 5 });
});

test("checkConfig accepts a clean check and uses null for an unlocated refusal", async () => {
  expect(await checkConfig("http://daemon", "", async () => Response.json({ ok: true, checked: true })))
    .toEqual({ ok: true });
  expect(await checkConfig("http://daemon", "", async () => Response.json({ error: "e" }, { status: 400 })))
    .toEqual({ ok: false, error: "e", line: null });
});

test("checkConfig returns HTTP and network failures as text", async () => {
  expect(await checkConfig("http://daemon", "", async () => Response.json({ error: "denied" }, { status: 401 })))
    .toEqual({ ok: false, error: "http://daemon/config answered 401: denied" });
  expect(await checkConfig("http://daemon", "", async () => { throw new Error("offline"); }))
    .toEqual({ ok: false, error: "offline" });
});

test("putConfig preserves a text save refusal's line", async () => {
  expect(await putConfig("http://daemon", { text: "[loop]\nworkers = 0\n" }, async () =>
    Response.json({ error: "e", line: 2 }, { status: 400 })))
    .toEqual({ ok: false, error: "e", refused: true, line: 2 });
});
