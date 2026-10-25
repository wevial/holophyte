import { expect, test } from "bun:test";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { buildConsole } from "../../build";
import { declarations } from "../cssTokens";

const moduleScript = (html: string) => /<script type="module"[^>]* src="(?:\.?\/)?([^"]+)"/.exec(html)?.[1];

test("one build emits the console at index.html alone, with its script, palette, fonts and the favicon", async () => {
  const outdir = `${await mkdtemp(`${tmpdir()}/console-dist-`)}/`;
  try {
    const listed = await buildConsole(outdir);
    const written = await readdir(outdir);
    for (const page of ["classic.html", "next.html"]) {
      expect(listed).not.toContain(page);
      expect(written).not.toContain(page);
    }
    expect(listed).toContain("index.html");
    expect(written).toContain("index.html");
    const html = await Bun.file(`${outdir}index.html`).text();
    expect(html).toMatch(/<link rel="icon" href="\/favicon\.svg"/);
    const script = moduleScript(html);
    expect(script).toBeDefined();
    expect(listed).toContain(script!);
    expect(written).toContain(script!);
    const document = new DOMParser().parseFromString(html, "text/html");
    for (const [preference, color] of [["light", "#eceaf3"], ["dark", "#0d0a15"]]) {
      expect(document.querySelector(`meta[name="theme-color"][media="(prefers-color-scheme: ${preference})"]`)
        ?.getAttribute("content")).toBe(color);
    }
    const stylesheets = [...document.querySelectorAll('link[rel="stylesheet"]')]
      .map((link) => link.getAttribute("href")!);
    const localStylesheets = stylesheets.filter((href) => !/^https?:/.test(href));
    expect(localStylesheets.length).toBeGreaterThan(0);
    const css = (await Promise.all(localStylesheets.map((href) =>
      Bun.file(outdir + href.replace(/^(?:\.\/|\/)/, "")).text()))).join("\n");
    expect(declarations(css).some(({ name }) => name === "--canvas")).toBe(true);
    expect(/--row-selected:\s*color-mix\(/.test(css)).toBe(true);
    const fonts = stylesheets.filter((href) => href.startsWith("https://fonts.googleapis.com/"));
    expect(fonts).toHaveLength(1);
    expect(new URL(fonts[0]!).searchParams.getAll("family")).toEqual([
      "Archivo:wdth,wght@100..125,400..800",
      "IBM Plex Mono:wght@400;500;600",
      "IBM Plex Sans:wght@400;500;600",
      "IBM Plex Serif:wght@400;500;600",
      "Atkinson Hyperlegible Next:wght@400;500;600",
    ]);
  } finally {
    await rm(outdir, { recursive: true, force: true });
  }
}, 30_000);
