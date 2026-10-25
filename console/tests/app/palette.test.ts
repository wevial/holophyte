import { describe, expect, test } from "bun:test";
import { Glob } from "bun";
import { declarations, resolveToken } from "../cssTokens";

const css = await Bun.file(new URL("../../src/app/theme.css", import.meta.url)).text();
const decls = declarations(css);
const dawn = [":root"];
const night = [':root[data-theme="dark"]'];
const systemDark = ["@media (prefers-color-scheme: dark)", ':root:not([data-theme="light"])'];

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
  test("theme tokens resolve to the approved Dawn, Night and system-dark values", () => {
    const expected = {
      dawn: {
        "--canvas": "#eceaf3", "--paper": "#fbfafd", "--ink": "#1d182b", "--ink-3": "#68617e",
        "--side": "#fbfafd", "--side-hover": "#eceaf3", "--act": "#af2f6e", "--act-wash": "#ffd8e5",
        "--ask": "#dcad21", "--ask-ink": "#7d6000", "--fact-warn": "#da7f00", "--p1": "#ffc1bb",
        "--tag-ink": "#38275a", "--focus": "#5a4ebf", "--bad-text": "#a6282a",
      },
      night: {
        "--canvas": "#0d0a15", "--paper": "#16121f", "--ink": "#e1dbf8", "--ink-3": "#938aae",
        "--side": "#1f192f", "--side-hover": "#0d0a15", "--act": "#f6a3c3", "--act-wash": "#2d1f2e",
        "--ask": "#f6cc62", "--ask-ink": "#ffe29b", "--fact-warn": "#ffa64d", "--p1": "#ff817a",
        "--tag-ink": "#eaebff", "--focus": "#6c63d6", "--bad-text": "#ffa39b",
      },
    };
    for (const [path, values] of [[dawn, expected.dawn], [night, expected.night], [systemDark, expected.night]] as const) {
      const resolved = Object.fromEntries(Object.keys(values).map((name) => [name, resolveToken(decls, path, name)]));
      expect(resolved).toEqual(values);
    }
  });

  test("the contrast function follows WCAG 2 at its known points", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#777777", "#ffffff")).toBeLessThan(4.5);
  });

  test("every named text reads at 4.5:1 or better on its ground in Dawn and Night", () => {
    const pairs = [
      ["--ink-3", "--paper-2"], ["--ink-3", "--side"], ["--ink-3", "--side-hover"],
      ["--act", "--act-wash"], ["--ink-3", "--act-wash"], ["--act-ink", "--act"], ["--bad-ink", "--bad"],
      ["--ok-ink", "--live-wash"], ["--bad-text", "--bad-wash"], ["--ask-ink", "--paper"], ["--warn-ink", "--paper"],
      ["--p1-ink", "--p1"], ["--p2-ink", "--p2"], ["--p3-ink", "--p3"], ["--tag-ink", "--tag"],
      ["--green-on", "--live"], ["--violet-on", "--wait"], ["--gold-on", "--ask"], ["--orange-on", "--fact-warn"],
    ] as const;
    const failing: string[] = [];
    for (const [theme, path] of [["Dawn", dawn], ["Night", night], ["system dark", systemDark]] as const) {
      for (const [text, ground] of pairs) {
        const ratio = contrast(resolveToken(decls, path, text), resolveToken(decls, path, ground));
        if (ratio < 4.5) failing.push(`${theme} ${text} on ${ground}: ${ratio.toFixed(2)}`);
      }
    }
    expect(failing).toEqual([]);
  });

  test("next components take their colours from theme tokens, leaving only the Appearance swatches as hex", async () => {
    expect(colourHits('className="bg-p1 text-[#171a2c]"')).toEqual(["#171a2c"]);
    const root = new URL("../../src/app/", import.meta.url).pathname;
    const hits: Record<string, string[]> = {};
    for await (const file of new Glob("**/*.{ts,tsx}").scan(root)) {
      const found = colourHits(await Bun.file(root + file).text());
      if (found.length > 0) hits[file] = [...new Set(found)].sort();
    }
    expect(hits).toEqual({
      "components/AppearanceTab.tsx": ["#0d0a15", "#16121f", "#af2f6e", "#eceaf3", "#f6a3c3", "#fbfafd"],
    });
  });
});
