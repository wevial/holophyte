import { expect, test } from "bun:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { buildConsole } from "../../build";

const moduleScript = (html: string) => /<script type="module"[^>]* src="(?:\.?\/)?([^"]+)"/.exec(html)?.[1];

test("one build emits the classic page and the new console's page, each with its own script and the favicon", async () => {
  const outdir = `${await mkdtemp(`${tmpdir()}/console-dist-`)}/`;
  try {
    const listed = await buildConsole(outdir);
    const written = await readdir(outdir);
    const scripts: string[] = [];
    for (const page of ["index.html", "next.html"]) {
      expect(listed).toContain(page);
      expect(written).toContain(page);
      const html = await Bun.file(outdir + page).text();
      expect(html).toMatch(/<link rel="icon" href="\/favicon\.svg"/);
      const script = moduleScript(html);
      expect(script).toBeDefined();
      expect(listed).toContain(script!);
      expect(written).toContain(script!);
      scripts.push(script!);
    }
    expect(scripts[0]).not.toBe(scripts[1]);
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}, 30_000);
