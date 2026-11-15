import { describe, expect, test } from "bun:test";
import { Glob } from "bun";
import { declarations, resolveToken } from "../cssTokens";

const css = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
const decls = declarations(css);
const lotusLight = [":root"];
const night = [':root[data-theme="dark"]'];
const systemDark = ["@media (prefers-color-scheme: dark)", ':root:not([data-theme="light"])'];

const TOKENS = ["--canvas", "--side", "--raised", "--hover", "--selected", "--ink", "--ink-2", "--ink-3", "--accent", "--focus"];
const TABLE: Record<string, string> = {
  "Lotus dark": "#0d0a15 #120f1c #15121f #1d1930 #352952 #ece8fb #b3adcc #857fa2 #b9a3ff #c9b4ff",
  "Dune dark": "#1b1817 #221e1d #252120 #2d2827 #4e3b30 #f3ece8 #c6bab4 #9a8d87 #ffad7a #ffbe96",
  "Tide dark": "#0b0e1d #10142a #131830 #1a2140 #273159 #e8ecfb #b0b8d6 #808aac #9db1ff #aabdff",
  "Mono dark": "#0a0a0b #111113 #141416 #1c1c20 #342a4c #ededf0 #b4b4bb #86868f #b9a3ff #c9b4ff",
  "Forest dark": "#0e1411 #131a16 #16201b #1e2a24 #253f34 #e8f1ec #b0c2b8 #84968c #7fd6b8 #9be3c8",
  "Lotus light": "#f6f5fa #ece8f5 #ffffff #e2dcf1 #c3bde6 #1d182b #4f4865 #655e7b #5a4ebf #5a4ebf",
  "Dune light": "#fcfaf7 #f4f0ea #ffffff #ede3df #dfc7b9 #1f1a1e #4e474b #655e62 #a4512e #a4512e",
  "Tide light": "#f5f7fb #eaeef6 #ffffff #dfe5f3 #bbc5ea #161b29 #444c62 #5a6278 #3651c9 #3651c9",
  "Mono light": "#fafafa #f0f0f1 #ffffff #e5e5e9 #ccc9e5 #18181b #45454c #5f5f68 #5a4ebf #5a4ebf",
  "Forest light": "#f6f8f4 #ecf1ea #ffffff #e0eadf #b7d2c5 #17201a #435047 #59665d #1f7a5c #1f7a5c",
};

/** Each theme as the cascade sees it: the rules that apply, most specific
 *  first, and a dark theme once under the Dark scheme and once under System. */
function variants(theme: string): [string, string[][]][] {
  const [name, mode] = theme.toLowerCase().split(" ") as [string, string];
  if (mode === "light") return [[theme, name === "lotus" ? [lotusLight] : [[`:root[data-light="${name}"]`], lotusLight]]];
  const pick = (base: string[], selector: string) => (name === "lotus" ? [base, lotusLight] : [[...base.slice(0, -1), selector], base, lotusLight]);
  return [
    [`${theme} (Dark)`, pick(night, `:root[data-theme="dark"][data-dark="${name}"]`)],
    [`${theme} (System)`, pick(systemDark, `:root:not([data-theme="light"])[data-dark="${name}"]`)],
  ];
}

const mixHex = (a: string, b: string, share: number) => `#${[1, 3, 5]
  .map((i) => Math.round(parseInt(a.slice(i, i + 2), 16) * share + parseInt(b.slice(i, i + 2), 16) * (1 - share)))
  .map((c) => c.toString(16).padStart(2, "0")).join("")}`;

function resolveIn(layers: string[][], name: string): string {
  for (const path of layers) {
    const key = path.join(" ");
    const value = decls.find((d) => d.name === name && d.path.join(" ") === key)?.value;
    if (value === undefined) continue;
    const reference = /^var\((--[a-z0-9-]+)\)$/.exec(value);
    if (reference) return resolveIn(layers, reference[1]!);
    const mix = /^color-mix\(in srgb, var\((--[a-z0-9-]+)\) (\d+)%, var\((--[a-z0-9-]+)\)\)$/.exec(value);
    if (mix) return mixHex(resolveIn(layers, mix[1]!), resolveIn(layers, mix[3]!), Number(mix[2]) / 100);
    return value;
  }
  throw new Error(`theme.css declares no ${name} under ${layers.map((path) => path.join(" ")).join(" | ")}`);
}

const THEMES = Object.keys(TABLE).flatMap(variants);

const luminance = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r! + 0.7152 * g! + 0.0722 * b!;
};

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi! + 0.05) / (lo! + 0.05);
}

const HEX = /#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])/g;
const RETIRED = /\b(?:text-ask(?![\w-])|outline-act(?![\w-]))/g;

function colourHits(source: string): string[] {
  return [...source.matchAll(HEX), ...source.matchAll(RETIRED)].map((match) => match[0]);
}

describe("the Hybrid palette", () => {
  test("each of the ten themes resolves its semantic tokens to the approved table", () => {
    for (const [theme, layers] of THEMES) {
      const expected = TABLE[theme.replace(/ \((?:Dark|System)\)$/, "")]!.split(" ");
      expect([theme, TOKENS.map((name) => resolveIn(layers, name))]).toEqual([theme, expected]);
    }
  });

  test("the contrast function follows WCAG 2 at its known points", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#777777", "#ffffff")).toBeLessThan(4.5);
  });

  test("in every theme the inks, state colours and focus ring read at their minimum ratio on their grounds", () => {
    const pairs: [string, string, number][] = [
      ...["--ink", "--ink-2", "--ink-3"].flatMap((ink) => ["--canvas", "--side", "--raised", "--hover"].map((ground) => [ink, ground, 4.5] as [string, string, number])),
      ["--ink", "--selected", 4.5],
      ...["--need", "--live", "--ask", "--wait", "--bad"].flatMap((state) => ["--canvas", "--raised"].map((ground) => [state, ground, 4.5] as [string, string, number])),
      ["--focus", "--canvas", 3], ["--focus", "--side", 3],
    ];
    const failing: string[] = [];
    for (const [theme, layers] of THEMES) {
      for (const [text, ground, minimum] of pairs) {
        const ratio = contrast(resolveIn(layers, text), resolveIn(layers, ground));
        if (ratio < minimum) failing.push(`${theme} ${text} on ${ground}: ${ratio.toFixed(2)}`);
      }
    }
    expect(THEMES).toHaveLength(15);
    expect(failing).toEqual([]);
  });

  test("every named text reads at 4.5:1 or better on its ground in every theme", () => {
    const pairs = [
      ["--ink-3", "--paper-2"], ["--ink-3", "--side"], ["--ink-3", "--side-hover"],
      ["--act", "--act-wash"], ["--ink-3", "--act-wash"], ["--act-ink", "--act"], ["--bad-ink", "--bad"],
      ["--ok-ink", "--live-wash"], ["--bad-text", "--bad-wash"], ["--ask-ink", "--paper"], ["--warn-ink", "--paper"],
      ["--p1-ink", "--p1"], ["--p2-ink", "--p2"], ["--p3-ink", "--p3"], ["--tag-ink", "--tag"],
      ["--green-on", "--live"], ["--violet-on", "--wait"], ["--gold-on", "--ask"], ["--orange-on", "--fact-warn"],
    ] as const;
    const failing: string[] = [];
    for (const [theme, layers] of THEMES) {
      for (const [text, ground] of pairs) {
        const ratio = contrast(resolveIn(layers, text), resolveIn(layers, ground));
        if (ratio < 4.5) failing.push(`${theme} ${text} on ${ground}: ${ratio.toFixed(2)}`);
      }
    }
    expect(failing).toEqual([]);
  });

  test("the project key colours resolve to the approved values and read at 4.5:1 on paper in every theme", () => {
    const names = ["--p-violet", "--p-teal", "--p-green", "--p-blue"];
    const expected = { light: ["#5a4ebf", "#00677a", "#006537", "#2f5fb3"], night: ["#a9a6ff", "#7fd6e6", "#7ef4ac", "#9db8ff"] };
    for (const [path, values] of [[lotusLight, expected.light], [night, expected.night], [systemDark, expected.night]] as const) {
      expect(names.map((name) => resolveToken(decls, path, name))).toEqual([...values]);
      for (const name of names) expect(contrast(resolveToken(decls, path, name), resolveToken(decls, path, "--paper"))).toBeGreaterThanOrEqual(4.5);
    }
  });

  test("next components, the Appearance tiles included, take their colours from theme tokens with no hex", async () => {
    expect(colourHits('className="bg-p1 text-[#171a2c]"')).toEqual(["#171a2c"]);
    const root = new URL("../../src/app/", import.meta.url).pathname;
    const hits: Record<string, string[]> = {};
    for await (const file of new Glob("**/*.{ts,tsx}").scan(root)) {
      const found = colourHits(await Bun.file(root + file).text());
      if (found.length > 0) hits[file] = [...new Set(found)].sort();
    }
    expect(hits).toEqual({});
  });
});
