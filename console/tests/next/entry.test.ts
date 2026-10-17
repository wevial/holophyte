import { expect, test } from "bun:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { buildConsole } from "../../build";
import { declarations } from "../cssTokens";

const moduleScript = (html: string) => /<script type="module"[^>]* src="(?:\.?\/)?([^"]+)"/.exec(html)?.[1];

test("one build emits the classic page and the new console's page, each with its own script, palette, fonts and the favicon", async () => {
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
      const document = new DOMParser().parseFromString(html, "text/html");
      const next = page === "next.html";
      for (const [preference, color] of [
        ["light", next ? "#eceaf3" : "#f4f1ea"],
        ["dark", next ? "#05070f" : "#141210"],
      ]) {
        expect(document.querySelector(`meta[name="theme-color"][media="(prefers-color-scheme: ${preference})"]`)
          ?.getAttribute("content")).toBe(color);
      }
      const stylesheets = [...document.querySelectorAll('link[rel="stylesheet"]')]
        .map((link) => link.getAttribute("href")!);
      const localStylesheets = stylesheets.filter((href) => !/^https?:/.test(href));
      expect(localStylesheets.length).toBeGreaterThan(0);
      const css = (await Promise.all(localStylesheets.map((href) =>
        Bun.file(outdir + href.replace(/^(?:\.\/|\/)/, "")).text()))).join("\n");
      expect(declarations(css).some(({ name }) => name === "--canvas")).toBe(next);
      if (next) {
        const fonts = stylesheets.filter((href) => href.startsWith("https://fonts.googleapis.com/"));
        expect(fonts).toHaveLength(1);
        expect(new URL(fonts[0]!).searchParams.getAll("family")).toEqual([
          "Archivo:wdth,wght@100..125,400..800",
          "IBM Plex Mono:wght@400;500;600",
          "IBM Plex Sans:wght@400;500;600",
          "IBM Plex Serif:wght@400;500;600",
          "Atkinson Hyperlegible Next:wght@400;500;600",
        ]);
      }
    }
    expect(scripts[0]).not.toBe(scripts[1]);
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}, 30_000);
