import { expect, test } from "bun:test";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildConsole } from "../build";

// `bun --cwd console run build` (the ticket's frozen form) is parsed by Bun
// 1.3 and 1.4 as `bun run` with no script, so it cannot witness the build;
// this test does, under the `bun --cwd console test` line that does run.
test("the build emits index.html wired to a built script and stylesheet", async () => {
  const outdir = `${mkdtempSync(join(tmpdir(), "holophyte-console-"))}/`;
  try {
    await buildConsole(outdir);
    const html = readFileSync(`${outdir}index.html`, "utf8");
    const script = html.match(/<script[^>]*src="\.\/([^"]+\.js)"/)?.[1];
    const stylesheet = html.match(/<link[^>]*rel="stylesheet"[^>]*href="\.\/([^"]+\.css)"/)?.[1];
    expect(script).toBeDefined();
    expect(stylesheet).toBeDefined();
    expect(existsSync(`${outdir}${script}`)).toBe(true);
    expect(existsSync(`${outdir}${stylesheet}`)).toBe(true);
  } finally {
    rmSync(outdir, { recursive: true, force: true });
  }
});
