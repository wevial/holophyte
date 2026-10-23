import { expect, test } from "bun:test";
import { declarations, resolveToken, tokensUnder } from "../cssTokens";

const css = await Bun.file(new URL("../../src/next/theme.css", import.meta.url)).text();
const classicCss = await Bun.file(new URL("../../src/theme.css", import.meta.url)).text();
const decls = declarations(css);
const dawnSelector = ":root";
const nightSelector = ':root[data-theme="dark"]';
const systemSelector = ':root:not([data-theme="light"])';
const media = "@media (prefers-color-scheme: dark)";

test("Dawn, Night and system-dark declare identical names and both dark blocks agree in value", () => {
  const dawn = tokensUnder(decls, dawnSelector);
  expect(dawn.length).toBeGreaterThan(20);
  expect(tokensUnder(decls, nightSelector)).toEqual(dawn);
  expect(tokensUnder(decls, systemSelector, media)).toEqual(dawn);
  const values = (selector: string) => Object.fromEntries(
    decls.filter((d) => d.path.at(-1) === selector).map((d) => [d.name, d.value]),
  );
  expect(values(systemSelector)).toEqual(values(nightSelector));
});

test("Tailwind exposes every raw colour and every classic colour through a declared raw token", () => {
  const raw = tokensUnder(decls, dawnSelector);
  const theme = new Map(decls.filter((d) => d.path.length === 1 && d.path[0] === "@theme inline")
    .map((d) => [d.name, d.value]));
  const unexposed = /^--(?:neutral|pink|red|orange|gold|yellow|green|violet|slate)-(?:1[0-2]|[1-9])$/;
  for (const name of raw.filter((name) => name !== "--card-shadow" && !unexposed.test(name))) {
    expect(theme.get(`--color-${name.slice(2)}`)).toBe(`var(${name})`);
  }
  const classicNames = tokensUnder(declarations(classicCss), "@theme inline")
    .filter((name) => name.startsWith("--color-"));
  expect(classicNames.length).toBeGreaterThan(20);
  for (const name of classicNames) {
    const reference = /^var\((--[a-z0-9-]+)\)$/.exec(theme.get(name) ?? "");
    expect(reference).not.toBeNull();
    expect(raw).toContain(reference![1]!);
  }
  expect(theme.get("--shadow-card")).toBe("var(--card-shadow)");
});

test("computed canvas and action colours honor explicit themes and otherwise follow system preference", () => {
  const root = document.documentElement;
  const device = (window as unknown as { happyDOM: { settings: { device: { prefersColorScheme: string } } } })
    .happyDOM.settings.device;
  const previousPreference = device.prefersColorScheme;
  const previousTheme = root.getAttribute("data-theme");
  const style = document.createElement("style");
  style.textContent = css.replace(/@import[^;]*;/, "");
  const token = (name: string) => getComputedStyle(root).getPropertyValue(name).trim();
  try {
    for (const [theme, preference, canvas, action] of [
      ["dark", "light", "#0d0a15", "#f6a3c3"],
      ["light", "dark", "#eceaf3", "#af2f6e"],
      [null, "dark", "#0d0a15", "#f6a3c3"],
      [null, "light", "#eceaf3", "#af2f6e"],
    ] as const) {
      device.prefersColorScheme = preference;
      if (theme === null) root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", theme);
      document.head.appendChild(style);
      expect(token("--canvas")).toBe(canvas);
      expect(token("--act")).toBe(action);
      style.remove();
    }
  } finally {
    style.remove();
    device.prefersColorScheme = previousPreference;
    if (previousTheme === null) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", previousTheme);
  }
});

test("the graph's edge tokens take the board's values in Dawn and in both dark blocks", () => {
  for (const [path, edge, dim] of [
    [[dawnSelector], "#857f97", "#bfbbce"],
    [[nightSelector], "#746b8f", "#40365a"],
    [[media, systemSelector], "#746b8f", "#40365a"],
  ] as const) {
    expect([resolveToken(decls, [...path], "--edge"), resolveToken(decls, [...path], "--edge-dim")]).toEqual([edge, dim]);
  }
});


test("reading text uses Plex Sans with Avenir fallbacks while headings keep Archivo", () => {
  const theme = new Map(decls.filter((d) => d.path.length === 1 && ["@theme", "@theme inline"].includes(d.path[0]!))
    .map((d) => [d.name, d.value]));
  expect(theme.get("--font-read")).toBe('"IBM Plex Sans", "Avenir Next", "Avenir", "Helvetica Neue", Arial, sans-serif');
  expect(theme.has("--font-plex")).toBe(false);
  expect(theme.get("--font-sans")).toStartWith('"Archivo"');
});
