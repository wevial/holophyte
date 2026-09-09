import { describe, expect, test } from "bun:test";

import { pollAll, readTokens } from "../poll.ts";
import { type Attention, type FetchResult, type Status, buildSummary } from "../tray.ts";

// The wire shapes from docs/reference/http.md.
const NOW = 1788450534491;
const STATUS: Status = {
  target: "/srv/dev/holophyte",
  project: "/srv/dev/holophyte",
  host: "writer-1",
  now: NOW,
  supervisor: { state: "live", heartbeat_age_ms: 8258 },
  thresholds: { heartbeat_stale_ms: 300000 },
  runs: [
    {
      ticket: "KO-219",
      phase: "working",
      heartbeat_age_ms: 12000,
    },
  ],
};
const QUIET: Attention = { level: "working", now: NOW, items: [] };
const ok = <T>(body: T): FetchResult<T> => ({ ok: true, body });
const labels = (items: { label?: string; type?: string }[]) =>
  items.filter((i) => i.type !== "separator").map((i) => i.label);

describe("buildSummary", () => {
  test("one working run and an empty /attention: nothing needs you, the project line, the hosts line, then the fixed entries", () => {
    const { items, level } = buildSummary(["127.0.0.1:7710"], { "127.0.0.1:7710": ok(STATUS) }, { "127.0.0.1:7710": ok(QUIET) }, NOW);
    expect(labels(items)).toEqual([
      "Nothing needs you",
      "holophyte · working KO-219 · hb 12s",
      "1 host · 1 daemon",
      "Show console",
      "Open at login",
      "Quit",
    ]);
    expect(level).toBe("working");
  });

  test("a blocked question and a failed run lead as two attention lines with kind, ticket and age", () => {
    const attention: Attention = {
      level: "attention",
      now: NOW,
      items: [
        { kind: "blocked", ticket: "KO-217", question: "keep the flag name?", asked_ms: NOW - 14 * 60_000, level: "attention" },
        { kind: "failed", ticket: "KO-218", reason: "verify failed", ended_ms: NOW - 2 * 3_600_000, attempt: 2, level: "attention" },
      ],
    };
    const { items, level } = buildSummary(["127.0.0.1:7710"], { "127.0.0.1:7710": ok(STATUS) }, { "127.0.0.1:7710": ok(attention) }, NOW);
    expect(labels(items).slice(0, 2)).toEqual([
      "holophyte · KO-217 · blocked 14m ago: keep the flag name?",
      "holophyte · KO-218 · failed 2h ago: verify failed",
    ]);
    expect(labels(items)).not.toContain("Nothing needs you");
    expect(level).toBe("attention");
  });

  test("clicking an attention or project line shows the console", () => {
    let shown = 0;
    const { items } = buildSummary(["127.0.0.1:7710"], { "127.0.0.1:7710": ok(STATUS) }, { "127.0.0.1:7710": ok(QUIET) }, NOW, {
      actions: { showConsole: () => (shown += 1) },
    });
    items.find((i) => i.label?.startsWith("holophyte · working"))?.click?.({ checked: false });
    expect(shown).toBe(1);
  });

  test("an unreachable daemon reads unreachable and the level is bad", () => {
    const { items, level } = buildSummary(
      ["127.0.0.1:7710", "writer-2:7710"],
      { "127.0.0.1:7710": ok(STATUS), "writer-2:7710": { ok: false, kind: "unreachable", error: "timeout" } },
      { "127.0.0.1:7710": ok(QUIET) },
      NOW,
    );
    expect(labels(items)).toContain("writer-2:7710 · unreachable");
    expect(labels(items)).toContain("1 host · 2 daemons");
    expect(level).toBe("bad");
  });
});

describe("pollAll with tokens from console.json", () => {
  test("a 401 daemon reads needs token while the address with a token is fetched with its bearer", async () => {
    const seen: Record<string, string | undefined> = {};
    const fakeFetch = async (url: string, init: { headers: Record<string, string> }): Promise<Response> => {
      const { host, pathname } = new URL(url);
      seen[`${host}${pathname}`] = init.headers.authorization;
      if (pathname === "/peers") return Response.json({ self: "127.0.0.1:7710", peers: ["writer-2:7710", "writer-3:7710"] });
      if (host === "writer-3:7710") return new Response("{}", { status: 401 });
      if (pathname === "/status") return Response.json({ ...STATUS, host });
      return Response.json(QUIET);
    };
    const tokens = readTokens(JSON.stringify({ tokens: { "writer-2:7710": "s3cret" } }), "/nowhere");
    const answer = await pollAll("http://127.0.0.1:7710/", tokens, { fetch: fakeFetch });
    const { items } = buildSummary(answer.peers, answer.statuses, answer.attentions, NOW);

    expect(labels(items)).toContain("writer-3:7710 · needs token");
    expect(seen["writer-2:7710/status"]).toBe("Bearer s3cret");
    expect(seen["writer-2:7710/attention"]).toBe("Bearer s3cret");
    expect(seen["127.0.0.1:7710/status"]).toBeUndefined();
    expect(seen["writer-3:7710/status"]).toBeUndefined();
  });

  test("token_files are read relative to the config directory and tokens win for the same address", () => {
    const files: Record<string, string> = { "/cfg/writer-2.token": "from-file\n", "/abs/writer-3.token": "abs\n" };
    const tokens = readTokens(
      JSON.stringify({
        tokens: { "writer-3:7710": "inline" },
        token_files: { "writer-2:7710": "writer-2.token", "writer-3:7710": "/abs/writer-3.token", "writer-4:7710": "missing" },
      }),
      "/cfg",
      (file) => {
        if (file in files) return files[file] as string;
        throw new Error(`ENOENT ${file}`);
      },
    );
    expect(tokens).toEqual({ "writer-2:7710": "from-file", "writer-3:7710": "inline" });
  });
});
