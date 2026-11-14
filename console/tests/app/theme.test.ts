import { expect, test } from "bun:test";
import { applyPick, applyTheme, readPick, writePick } from "../../src/lib/theme";
import { declarations, resolveToken, tokensUnder } from "../cssTokens";

const css = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
const decls = declarations(css);
const lotusLightSelector = ":root";
const nightSelector = ':root[data-theme="dark"]';
const systemSelector = ':root:not([data-theme="light"])';
const media = "@media (prefers-color-scheme: dark)";

test("Lotus light, Lotus dark and system-dark declare identical names and both dark blocks agree in value", () => {
  const sharedDerived = ["--hair", "--hair-2", "--need-edge", "--need-tint"];
  const lotusLight = tokensUnder(decls, lotusLightSelector).filter((name) => !sharedDerived.includes(name));
  expect(lotusLight.length).toBeGreaterThan(20);
  expect(tokensUnder(decls, nightSelector)).toEqual(lotusLight);
  expect(tokensUnder(decls, systemSelector, media)).toEqual(lotusLight);
  const values = (selector: string) => Object.fromEntries(
    decls.filter((d) => d.path.at(-1) === selector).map((d) => [d.name, d.value]),
  );
  expect(values(systemSelector)).toEqual(values(nightSelector));
  for (const pick of ["dune", "tide", "mono", "forest"]) {
    expect(values(`:root[data-theme="dark"][data-dark="${pick}"]`))
      .toEqual(values(`:root:not([data-theme="light"])[data-dark="${pick}"]`));
  }
});

test("Tailwind exposes every raw colour through a declared raw token", () => {
  const raw = tokensUnder(decls, lotusLightSelector);
  const theme = new Map(decls.filter((d) => d.path.length === 1 && d.path[0] === "@theme inline")
    .map((d) => [d.name, d.value]));
  const unexposed = /^--(?:neutral|pink|red|orange|gold|yellow|green|violet|slate)-(?:1[0-2]|[1-9])$/;
  const notColours = ["--card-shadow", "--need-tint", "--need-tint-mix", "--accent-tint-mix", "--need-edge-mix", "--grain", "--grain-side"];
  for (const name of raw.filter((name) => !notColours.includes(name) && !unexposed.test(name))) {
    expect(theme.get(`--color-${name.slice(2)}`)).toBe(`var(${name})`);
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
      ["dark", "light", "#0d0a15", "#b9a3ff"],
      ["light", "dark", "#f6f5fa", "#5a4ebf"],
      [null, "dark", "#0d0a15", "#b9a3ff"],
      [null, "light", "#f6f5fa", "#5a4ebf"],
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

type Scheme = "dark" | "light" | null;

function canvasUnder(scheme: Scheme, preference: "light" | "dark"): string {
  const root = document.documentElement;
  const device = (window as unknown as { happyDOM: { settings: { device: { prefersColorScheme: string } } } })
    .happyDOM.settings.device;
  const previousPreference = device.prefersColorScheme;
  const style = document.createElement("style");
  style.textContent = css.replace(/@import[^;]*;/, "");
  try {
    device.prefersColorScheme = preference;
    applyTheme(scheme ?? "system");
    document.head.appendChild(style);
    return getComputedStyle(root).getPropertyValue("--canvas").trim();
  } finally {
    style.remove();
    device.prefersColorScheme = previousPreference;
    applyTheme("system");
  }
}

function withPicks<T>(body: () => T): T {
  localStorage.clear();
  try {
    return body();
  } finally {
    localStorage.clear();
    applyPick("light", "lotus");
    applyPick("dark", "lotus");
  }
}

test("with nothing stored both picks are Lotus and the canvas follows the system preference", () => {
  withPicks(() => {
    expect([readPick("light"), readPick("dark")]).toEqual(["lotus", "lotus"]);
    applyPick("light", readPick("light"));
    applyPick("dark", readPick("dark"));
    expect([canvasUnder(null, "light"), canvasUnder(null, "dark")]).toEqual(["#f6f5fa", "#0d0a15"]);
  });
});

test("a Tide dark pick and a Dune light pick follow the system preference, and the Light scheme ignores the dark pick", () => {
  withPicks(() => {
    applyPick("dark", "tide");
    applyPick("light", "dune");
    expect([canvasUnder(null, "dark"), canvasUnder(null, "light")]).toEqual(["#0b0e1d", "#fcfaf7"]);
    expect([canvasUnder("light", "dark"), canvasUnder("dark", "light")]).toEqual(["#fcfaf7", "#0b0e1d"]);
  });
});

test("written picks survive a fresh read, and with storage throwing they still apply while the read falls back to Lotus", () => {
  withPicks(() => {
    writePick("light", "forest");
    writePick("dark", "mono");
    expect([localStorage.getItem("holophyte.theme.light"), localStorage.getItem("holophyte.theme.dark")]).toEqual(["forest", "mono"]);
    expect([readPick("light"), readPick("dark")]).toEqual(["forest", "mono"]);
    const real = Object.getOwnPropertyDescriptor(globalThis, "localStorage")!;
    Object.defineProperty(globalThis, "localStorage", { configurable: true, get: () => { throw new Error("storage disabled"); } });
    try {
      writePick("light", "dune");
      writePick("dark", "tide");
      applyPick("light", "dune");
      applyPick("dark", "tide");
      expect([canvasUnder("light", "dark"), canvasUnder("dark", "light")]).toEqual(["#fcfaf7", "#0b0e1d"]);
      expect([readPick("light"), readPick("dark")]).toEqual(["lotus", "lotus"]);
    } finally {
      Object.defineProperty(globalThis, "localStorage", real);
    }
  });
});

test("the graph's edge tokens take the board's values in Lotus light and in both dark blocks", () => {
  for (const [path, edge, dim] of [
    [[lotusLightSelector], "#857f97", "#bfbbce"],
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
