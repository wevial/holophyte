import { expect, test } from "bun:test";
import { oklchToHex } from "../../src/lib/oklch";
import { applyPick, applyTheme, deriveTheme, readPick, writePick, type ThemeMode, type ThemePick, type ThemeSpec } from "../../src/lib/theme";
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
  const notColours = ["--nav-on", "--card-shadow", "--need-tint", "--need-tint-mix", "--accent-tint-mix", "--need-edge-mix", "--grain", "--grain-side"];
  for (const name of raw.filter((name) => !notColours.includes(name) && !unexposed.test(name) && !name.startsWith("--haze-"))) {
    expect(theme.get(`--color-${name.slice(2)}`)).toBe(`var(${name})`);
  }
  expect(theme.get("--shadow-card")).toBe("var(--card-shadow)");
});

test("computed canvas and action colours honor explicit themes and otherwise follow system preference", () => {
  for (const [theme, preference, canvas, action] of [
    ["dark", "light", "#0d0a15", "#b9a3ff"],
    ["light", "dark", "#fbfaff", "#5a4ebf"],
    [null, "dark", "#0d0a15", "#b9a3ff"],
    [null, "light", "#fbfaff", "#5a4ebf"],
  ] as const) {
    expect([tokenUnder(theme, preference), tokenUnder(theme, preference, "--act")]).toEqual([canvas, action]);
  }
});

function tokenUnder(scheme: "dark" | "light" | null, preference: "light" | "dark", token = "--canvas"): string {
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
    return getComputedStyle(root).getPropertyValue(token).trim();
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
    expect([tokenUnder(null, "light"), tokenUnder(null, "dark")]).toEqual(["#fbfaff", "#0d0a15"]);
  });
});

test("a Tide dark pick and a Dune light pick follow the system preference, and each explicit scheme ignores the other pick", () => {
  withPicks(() => {
    applyPick("dark", "tide");
    applyPick("light", "dune");
    expect([tokenUnder(null, "dark"), tokenUnder(null, "light")]).toEqual(["#0b0e1d", "#fcfaf7"]);
    expect([tokenUnder("light", "dark"), tokenUnder("dark", "light")]).toEqual(["#fcfaf7", "#0b0e1d"]);
    applyPick("dark", "lotus");
    expect([tokenUnder("dark", "light"), tokenUnder(null, "dark")]).toEqual(["#0d0a15", "#0d0a15"]);
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
      expect([tokenUnder("light", "dark"), tokenUnder("dark", "light")]).toEqual(["#fcfaf7", "#0b0e1d"]);
      expect([readPick("light"), readPick("dark")]).toEqual(["lotus", "lotus"]);
    } finally {
      Object.defineProperty(globalThis, "localStorage", real);
    }
  });
});

const HAZE_TOKENS = ["--haze-ground", "--haze-back", "--haze-front", "--haze-strength", "--haze-fallback-back", "--haze-fallback-front"];
const HAZE: Record<string, string> = {
  "lotus dark": "#0d0a15 #4b3fc4 #c77ee8 0.30 rgba(91,74,230,.34) rgba(199,126,232,.2)",
  "dune dark": "#1b1817 #8a2f4f #e8785a 0.32 rgba(138,47,79,.42) rgba(232,120,90,.22)",
  "tide dark": "#0b0e1d #2c3cb0 #8a6bff 0.38 rgba(44,60,176,.42) rgba(138,107,255,.22)",
  "mono dark": "#0a0a0b #4b3fc4 #d07ee8 0.32 rgba(75,63,196,.34) rgba(208,126,232,.2)",
  "forest dark": "#0e1411 #16594a #b4d368 0.28 rgba(22,89,74,.55) rgba(180,211,104,.18)",
  "lotus light": "#fbfaff #c8bff6 #f3bddb 0.62 rgba(200,191,246,.7) rgba(243,189,219,.55)",
  "dune light": "#fcfaf7 #f6c4ad #f0b5c8 0.62 rgba(246,196,173,.75) rgba(240,181,200,.6)",
  "tide light": "#f5f7fb #bcd0f5 #cfc4f6 0.62 rgba(188,208,245,.75) rgba(207,196,246,.55)",
  "mono light": "#fafafa #d4ccf3 #f2c3dc 0.62 rgba(212,204,243,.7) rgba(242,195,220,.5)",
  "forest light": "#f6f8f4 #93cdb6 #e8f0a2 0.62 rgba(147,205,182,.75) rgba(232,240,162,.6)",
};

test("each of the ten themes resolves its haze colours, strength and fallback gradients to the approved table", () => {
  const compact = (value: string) => value.replace(/\s+/g, "").replace(/(^|[(,])0\./g, "$1.");
  withPicks(() => {
    for (const [theme, row] of Object.entries(HAZE)) {
      const [pick, mode] = theme.split(" ") as [ThemePick, ThemeMode];
      applyPick(mode, pick);
      const other = mode === "dark" ? "light" : "dark";
      const resolved = HAZE_TOKENS.map((token) => compact(tokenUnder(mode, other, token)));
      expect([theme, resolved]).toEqual([theme, row.split(" ").map(compact)]);
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

test("OKLCH converts to the CSS Color 4 reference points for white, black and sRGB red", () => {
  expect([oklchToHex({ l: 1, c: 0, h: 0 }), oklchToHex({ l: 0, c: 0, h: 0 }), oklchToHex({ l: 0.628, c: 0.2577, h: 29.23 })])
    .toEqual(["#ffffff", "#000000", "#ff0000"]);
});

const spec = (ground: ThemeSpec["ground"], haze: ThemeSpec["haze"] = { source: "hand", back: 280, front: 315 }): ThemeSpec =>
  ({ name: "Test", ground, accent: { l: 0.77, c: 0.13, h: 300 }, haze, grain: 4 });

test("a ground under L 0.5 derives a dark theme with the sidebar a step lighter and muted ink at L 0.66; a ground at L 0.97 is light", () => {
  const dark = deriveTheme(spec({ l: 0.15, c: 0.03, h: 290 }));
  expect(dark.mode).toBe("dark");
  expect(dark.colours.side.l).toBeCloseTo(0.175, 6);
  expect(dark.colours.side.c).toBeCloseTo(0.04, 6);
  expect(dark.colours["ink-3"].l).toBe(0.66);
  expect(deriveTheme(spec({ l: 0.97, c: 0.01, h: 290 })).mode).toBe("light");
});

test("haze from the accent puts the back band at the accent's hue and the front band 45 degrees on", () => {
  const { haze } = deriveTheme(spec({ l: 0.15, c: 0.03, h: 290 }, { source: "accent", back: 10, front: 20 }));
  expect([haze.back.h, haze.front.h]).toEqual([300, 345]);
});

test("the current page's nav-on gradient takes the board's values in Lotus and every other built-in or custom pick draws it from its own focus", () => {
  const lotusDark = "linear-gradient(90deg, rgba(164, 123, 255, 0.24), rgba(164, 123, 255, 0.05))";
  expect([
    resolveToken(decls, [lotusLightSelector], "--nav-on"),
    resolveToken(decls, [nightSelector], "--nav-on"),
    resolveToken(decls, [media, systemSelector], "--nav-on"),
  ]).toEqual(["linear-gradient(90deg, rgba(90, 78, 191, 0.28), rgba(90, 78, 191, 0.16))", lotusDark, lotusDark]);
  const fromFocus = (start: string, end: string) =>
    `linear-gradient(90deg, rgb(from var(--focus) r g b / ${start}), rgb(from var(--focus) r g b / ${end}))`;
  for (const pick of ["dune", "tide", "mono", "forest", "custom"]) {
    for (const [path, gradient] of [
      [[`:root[data-light="${pick}"]`], fromFocus("0.28", "0.16")],
      [[`:root[data-theme="dark"][data-dark="${pick}"]`], fromFocus("0.24", "0.05")],
      [[media, `:root:not([data-theme="light"])[data-dark="${pick}"]`], fromFocus("0.24", "0.05")],
    ] as const) {
      if (pick !== "custom") expect(resolveToken(decls, [...path], "--focus")).toMatch(/^#[0-9a-f]{6}$/);
      expect([pick, resolveToken(decls, [...path], "--nav-on")]).toEqual([pick, gradient]);
    }
  }
});
